"""Admin data editor — reads and audited, revertible edits on the DuckDB.

Pure DuckDB logic (no HTTP, no GCS): tools/robotics-admin/main.py handles
auth, pulling/pushing the authoritative DB and refreshing Firestore.

Every edit:
  - checks `expect` (the values the admin saw) against the row first, so a
    stale screen can never overwrite a newer change (raises EditConflict);
  - records before/after in admin_edits;
  - is revertible (except merge, flagged revertible=false).
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

import duckdb

from . import direction, entities, hashtags, subtypes

LIVE = ("active", "materialized")
EDGE_STATUSES = ("active", "materialized", "invalidated", "rejected", "internal")   # every status a relationship can hold
# 'internal' = an internal event: a self-loop kept outside every live view (docs/ticket-internal-events.md)
LINK_RELATIONS = ("subsidiary_of", "sister_of")
# "group" = a generic bucket ("Venture Capital", "European robotics companies"):
# kept as an entity so later mentions match it for free, but not a company.
ENTITY_TYPES = ("public_company", "private_company", "government", "organization", "group")


class EditError(ValueError):
    """Bad request: unknown op, missing field, invalid value."""


class EditConflict(RuntimeError):
    """The record changed since the admin loaded it."""


_DDL = """
CREATE TABLE IF NOT EXISTS admin_edits (
    edit_id     VARCHAR PRIMARY KEY,
    edited_at   TIMESTAMP DEFAULT now(),
    email       VARCHAR NOT NULL,
    ip          VARCHAR,
    op          VARCHAR NOT NULL,
    payload     VARCHAR NOT NULL,     -- JSON request
    before      VARCHAR,              -- JSON
    after       VARCHAR,              -- JSON
    revertible  BOOLEAN NOT NULL,
    reverts     VARCHAR,              -- edit_id this edit undid
    reverted_by VARCHAR               -- edit_id that undid this one
)
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_DDL)
    # Alias trust columns (same migration as db.init_schema): admin alias
    # writes are verified, and the admin service may run before any ingest has.
    con.execute("ALTER TABLE entity_aliases ADD COLUMN IF NOT EXISTS verified BOOLEAN")
    con.execute("ALTER TABLE entity_aliases ADD COLUMN IF NOT EXISTS source VARCHAR")
    con.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_subtype VARCHAR")   # src/subtypes.py


# ── Reads ──────────────────────────────────────────────────────────

def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _bare_brand(alias: str, name: str) -> bool:
    """'Amazon' for 'Amazon Web Services': one word that only repeats the
    first word of a longer name — it names the brand, not this unit."""
    a, n = _words(alias), _words(name)
    return len(a) == 1 and len(n) >= 2 and a[0] == n[0]


def _is_unit(con: duckdb.DuckDBPyConnection, eid: int) -> bool:
    """Has a parent or sisters — i.e. one business of a larger group."""
    return bool(con.execute("""SELECT 1 FROM entity_links WHERE (child_id = ? AND relation = 'subsidiary_of')
                               OR (relation = 'sister_of' AND ? IN (child_id, parent_id)) LIMIT 1""",
                            [eid, eid]).fetchone())


def _distinctive_names(con: duckdb.DuckDBPyConnection, eid: int, name: str) -> list[str]:
    """Names that point at THIS entity. A bare brand ("Toyota") is the group's
    own name, so it counts for a top-level entity but not for one of its units
    ("Amazon" doesn't mean Amazon Web Services)."""
    aliases = [r[0] for r in con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?", [eid]).fetchall()]
    unit = _is_unit(con, eid)
    return [name] + [a for a in aliases if a != name and not (unit and _bare_brand(a, name))]


def _mentions(text: str, names: list[str]) -> bool:
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n.lower()) + r"(?![a-z0-9])", t) for n in names if n)


def list_entities(con: duckdb.DuckDBPyConnection, q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    like = f"%{q.strip()}%" if q else "%"
    rows = con.execute("""
        SELECT e.entity_id, e.name, e.type, e.ticker,
               (SELECT COUNT(*) FROM entity_aliases a WHERE a.entity_id = e.entity_id) AS aliases,
               (SELECT COUNT(DISTINCT r.catalyst_id) FROM relationships r
                 WHERE e.entity_id IN (r.entity_a_id, r.entity_b_id) AND r.status IN ('active','materialized')) AS cards
        FROM entities e
        WHERE e.name ILIKE ? OR e.entity_id IN (SELECT entity_id FROM entity_aliases WHERE alias ILIKE ?)
        ORDER BY cards DESC, e.name LIMIT ?""", [like, like, limit]).fetchall()
    return [{"id": i, "name": n, "type": t, "ticker": tk, "aliases": a, "cards": c} for i, n, t, tk, a, c in rows]


def entity_detail(con: duckdb.DuckDBPyConnection, eid: int) -> dict[str, Any]:
    row = con.execute("SELECT entity_id, name, type, ticker FROM entities WHERE entity_id = ?", [eid]).fetchone()
    if not row:
        raise EditError(f"no entity {eid}")
    _, name, typ, ticker = row
    aliases = []
    for (alias,) in con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", [eid]).fetchall():
        others = [o for (o,) in con.execute("""SELECT e.name FROM entity_aliases a JOIN entities e USING (entity_id)
                                               WHERE lower(a.alias) = lower(?) AND a.entity_id <> ?""", [alias, eid])
                  .fetchall()]
        aliases.append({"alias": alias, "bare_brand": _bare_brand(alias, name), "also_on": others})
    family = [{"relation": rel, "role": role, "id": oid, "name": oname} for rel, role, oid, oname in con.execute("""
        SELECT l.relation, 'parent', l.parent_id, e.name FROM entity_links l JOIN entities e ON e.entity_id = l.parent_id
         WHERE l.child_id = ? AND l.relation = 'subsidiary_of'
        UNION ALL SELECT l.relation, 'child', l.child_id, e.name FROM entity_links l JOIN entities e ON e.entity_id = l.child_id
         WHERE l.parent_id = ? AND l.relation = 'subsidiary_of'
        UNION ALL SELECT 'sister_of', 'sister', CASE WHEN l.child_id = ? THEN l.parent_id ELSE l.child_id END, e.name
          FROM entity_links l JOIN entities e ON e.entity_id = CASE WHEN l.child_id = ? THEN l.parent_id ELSE l.child_id END
         WHERE l.relation = 'sister_of' AND ? IN (l.child_id, l.parent_id)
        ORDER BY 2, 4""", [eid, eid, eid, eid, eid]).fetchall()]
    names = _distinctive_names(con, eid, name)
    rels = []
    for (rel_id, a_id, a_name, rt, b_id, b_name, ev, conf, status, mech, entry_id, date, headline, finding) in con.execute("""
            SELECT r.rel_id, r.entity_a_id, ea.name, r.rel_type, r.entity_b_id, eb.name, r.evidence_type, r.confidence,
                   r.status, r.mechanism, c.entry_id, c.timestamp, c.headline, c.raw_finding
            FROM relationships r JOIN catalysts c USING (catalyst_id)
            JOIN entities ea ON ea.entity_id = r.entity_a_id JOIN entities eb ON eb.entity_id = r.entity_b_id
            WHERE ? IN (r.entity_a_id, r.entity_b_id) AND r.entity_a_id <> r.entity_b_id
            ORDER BY c.timestamp DESC, r.rel_id""", [eid]).fetchall():
        rels.append({"rel_id": rel_id, "a": {"id": a_id, "name": a_name}, "rel_type": rt, "b": {"id": b_id, "name": b_name},
                     "evidence": ev, "confidence": conf, "status": status, "mechanism": mech, "entry_id": entry_id,
                     "date": date.isoformat() if date else None, "headline": headline, "finding": finding,
                     "named_in_card": _mentions(f"{headline} {finding}", names)})
    # Pre-tick support: per alias, the live edges on cards that reached this
    # entity ONLY through that alias (card never names the entity itself).
    for al in aliases:
        al["via_rel_ids"] = _via(rels, name, al["alias"])
    return {"id": eid, "name": name, "type": typ, "ticker": ticker, "aliases": aliases, "family": family,
            "relationships": rels}


def _rename_entity(con: duckdb.DuckDBPyConnection, eid: int, new_name: str) -> None:
    """Rename keeping entity_id. DuckDB rejects updating a UNIQUE column (name)
    of a row other rows reference by foreign key (aliases, relationships), so
    those rows are parked on a throw-away placeholder, the entity renamed,
    and the rows moved back. Each statement autocommits; any failure raises
    and the admin service discards its working copy (nothing is pushed)."""
    refs = con.execute("""SELECT (SELECT COUNT(*) FROM relationships WHERE ? IN (entity_a_id, entity_b_id))
                               + (SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ?)""", [eid, eid]).fetchone()[0]
    if not refs:
        con.execute("UPDATE entities SET name = ? WHERE entity_id = ?", [new_name, eid])
        return
    typ, = con.execute("SELECT type FROM entities WHERE entity_id = ?", [eid]).fetchone()
    tmp = con.execute("INSERT INTO entities (name, type) VALUES (?, ?) RETURNING entity_id",
                      [f"__rename_placeholder_{eid}_{uuid.uuid4().hex[:8]}", typ]).fetchone()[0]
    for src, dst in ((eid, tmp), (tmp, eid)):
        if src == tmp:
            con.execute("UPDATE entities SET name = ? WHERE entity_id = ?", [new_name, eid])
        for col in ("entity_a_id", "entity_b_id"):
            con.execute(f"UPDATE relationships SET {col} = ? WHERE {col} = ?", [dst, src])
        con.execute("UPDATE entity_aliases SET entity_id = ? WHERE entity_id = ?", [dst, src])
    con.execute("DELETE FROM entities WHERE entity_id = ?", [tmp])


def _rel_rows(con: duckdb.DuckDBPyConnection, where: str, params: list) -> list[dict[str, Any]]:
    """Full relationship rows (every column), JSON-safe — what a hard delete must keep to be undone."""
    cols = [c for (c,) in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = "
                                      "'relationships' ORDER BY ordinal_position").fetchall()]
    rows = []
    for r in con.execute(f"SELECT * FROM relationships WHERE {where} ORDER BY rel_id", params).fetchall():
        rows.append({c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, r)})
    return rows


def _restore_rel_rows(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> set[int]:
    """Re-insert hard-deleted relationship rows exactly (same rel_id). Refuses if any id is reused or an
    endpoint / card is gone — nothing is written then."""
    for r in rows:
        if con.execute("SELECT 1 FROM relationships WHERE rel_id = ?", [r["rel_id"]]).fetchone():
            raise EditConflict(f"relationship id {r['rel_id']} is in use again — restore by hand")
        for k in ("entity_a_id", "entity_b_id"):
            if not con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [r[k]]).fetchone():
                raise EditConflict(f"relationship {r['rel_id']}: entity {r[k]} no longer exists")
        if not con.execute("SELECT 1 FROM catalysts WHERE catalyst_id = ?", [r["catalyst_id"]]).fetchone():
            raise EditConflict(f"relationship {r['rel_id']}: its card no longer exists")
        # Same rule as repoint_edge / set_edge_status: never re-create a live twin (dedupe would then
        # silently delete one of the two). Symmetric types: the reversed pair is a twin too.
        if r.get("status") in LIVE:
            from .direction import symmetric_types  # noqa: PLC0415
            pairs = [(r["entity_a_id"], r["entity_b_id"])]
            if r["rel_type"] in symmetric_types():
                pairs.append((r["entity_b_id"], r["entity_a_id"]))
            for a, b in pairs:
                if con.execute("""SELECT 1 FROM relationships WHERE catalyst_id = ? AND entity_a_id = ? AND rel_type = ?
                                  AND entity_b_id = ? AND status IN ('active', 'materialized')""",
                               [r["catalyst_id"], a, r["rel_type"], b]).fetchone():
                    raise EditConflict(f"relationship {r['rel_id']}: the same edge exists on its card again — "
                                       "reject or delete that one first")
    for r in rows:
        cols = list(r)
        con.execute(f"INSERT INTO relationships ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [r[c] for c in cols])
    return {r["catalyst_id"] for r in rows}


def _live_catalysts(con: duckdb.DuckDBPyConnection, eid: int) -> set[int]:
    return {r[0] for r in con.execute("SELECT DISTINCT catalyst_id FROM relationships WHERE ? IN "
                                      "(entity_a_id, entity_b_id) AND status IN ('active', 'materialized')",
                                      [eid]).fetchall()}


def _any_catalysts(con: duckdb.DuckDBPyConnection, eid: int) -> set[int]:
    return {r[0] for r in con.execute("SELECT DISTINCT catalyst_id FROM relationships WHERE ? IN "
                                      "(entity_a_id, entity_b_id)", [eid]).fetchall()}


def _via(rels: list[dict[str, Any]], name: str, alias: str) -> list[int]:
    return [r["rel_id"] for r in rels if r["status"] in LIVE
            and entities.only_via_alias(f"{r['headline']} {r['finding'] or ''}", name, alias)]


def _live_rels_with_text(con: duckdb.DuckDBPyConnection, eid: int) -> list[dict[str, Any]]:
    return [{"rel_id": i, "status": st, "headline": h, "finding": f} for i, st, h, f in con.execute("""
        SELECT r.rel_id, r.status, c.headline, c.raw_finding FROM relationships r JOIN catalysts c USING (catalyst_id)
        WHERE ? IN (r.entity_a_id, r.entity_b_id) AND r.entity_a_id <> r.entity_b_id
          AND r.status IN ('active', 'materialized')""", [eid]).fetchall()]


def card_entities(con: duckdb.DuckDBPyConnection, entry_id: str) -> dict[str, Any]:
    """Pick-list for fixing an edge on one card (read-only, no LLM):
    - on_card: every endpoint of the card's live edges; `new` = created from
      this card (ingest logged it as a new name for this catalyst);
    - in_text: existing entities whose name or a capitalized alias appears in
      the card's story (case-sensitive, whole words) — catches the right company even when
      extraction resolved the mention to a wrong one (SEER vs Serve)."""
    row = con.execute("SELECT catalyst_id, headline, raw_finding FROM catalysts WHERE entry_id = ?", [entry_id]).fetchone()
    if not row:
        raise EditError(f"no card {entry_id!r}")
    cid, head, finding = row
    new_names = {n.lower() for (n,) in con.execute(
        "SELECT suggested_name FROM unresolved_entities WHERE catalyst_id = ? AND suggested_name IS NOT NULL "
        "AND COALESCE(suggested_type, '') NOT IN ('generic_discarded', 'ungrounded_discarded')", [cid]).fetchall()}
    on_card = [{"id": i, "name": n, "type": t, "new": n.lower() in new_names} for i, n, t in con.execute("""
        SELECT DISTINCT e.entity_id, e.name, e.type FROM relationships r JOIN entities e
          ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
        WHERE r.catalyst_id = ? AND r.status IN ('active', 'materialized') ORDER BY e.name""", [cid]).fetchall()]
    # Proper names only, matched case-sensitively: an all-lowercase label
    # ("robotics", "humanoid robot") is a generic phrase, and "Humanoid" the
    # company must not match "humanoid robots". Story text only: headlines are
    # Title Case, so every word there looks like a proper name.
    text = finding or ""
    seen = {e["id"] for e in on_card}
    hits: dict[int, dict[str, Any]] = {}
    for eid, name, typ, label in con.execute("""
            SELECT e.entity_id, e.name, e.type, e.name FROM entities e WHERE e.type <> 'group'
            UNION ALL SELECT e.entity_id, e.name, e.type, a.alias FROM entity_aliases a JOIN entities e USING (entity_id)
            WHERE e.type <> 'group'""").fetchall():
        if eid in seen or eid in hits or len(label) < 3 or label == label.lower():
            continue
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])", text):
            hits[eid] = {"id": eid, "name": name, "type": typ, "via": None if label == name else label}
    return {"entry_id": entry_id, "on_card": on_card,
            "in_text": sorted(hits.values(), key=lambda h: h["name"].lower())}


def scan(con: duckdb.DuckDBPyConnection, min_cards: int = 5, max_rate: float = 0.3) -> dict[str, Any]:
    """Free, rule-based suspects (no LLM):
    - entities in a corporate family whose own name (or a distinctive alias)
      appears in few of their cards — news about a sister/parent likely
      landed on them through a brand alias ("Amazon" → AWS);
    - aliases recorded on more than one entity."""
    fam = {r[0] for r in con.execute("SELECT child_id FROM entity_links UNION SELECT parent_id FROM entity_links")
           .fetchall()}
    suspects = []
    for eid, name in con.execute("SELECT entity_id, name FROM entities ORDER BY name").fetchall():
        if eid not in fam:
            continue
        cards = con.execute("""SELECT DISTINCT c.catalyst_id, c.headline || ' ' || c.raw_finding FROM relationships r
                               JOIN catalysts c USING (catalyst_id)
                               WHERE ? IN (r.entity_a_id, r.entity_b_id) AND r.status IN ('active','materialized')""",
                            [eid]).fetchall()
        if len(cards) < min_cards:
            continue
        names = _distinctive_names(con, eid, name)
        named = sum(1 for _c, text in cards if _mentions(text, names))
        rate = named / len(cards)
        if rate <= max_rate:
            suspects.append({"id": eid, "name": name, "cards": len(cards), "named_in": named, "rate": round(rate, 2)})
    shared = [{"alias": a, "entities": sorted(set(n.split("\x1f")))} for a, n in con.execute("""
        SELECT lower(a.alias), string_agg(e.name, '\x1f') FROM entity_aliases a JOIN entities e USING (entity_id)
        GROUP BY 1 HAVING COUNT(DISTINCT a.entity_id) > 1 ORDER BY 1""").fetchall()]
    return {"suspect_entities": sorted(suspects, key=lambda s: (s["rate"], -s["cards"])), "shared_aliases": shared}


def edits(con: duckdb.DuckDBPyConnection, limit: int = 50) -> list[dict[str, Any]]:
    cols = ["edit_id", "edited_at", "email", "ip", "op", "payload", "before", "after", "revertible", "reverts", "reverted_by"]
    out = []
    for row in con.execute(f"SELECT {', '.join(cols)} FROM admin_edits ORDER BY edited_at DESC LIMIT ?", [limit]).fetchall():
        d = dict(zip(cols, row))
        d["edited_at"] = d["edited_at"].isoformat() if d["edited_at"] else None
        for k in ("payload", "before", "after"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out


# ── Edits ──────────────────────────────────────────────────────────

def _need(p: dict[str, Any], *keys: str) -> list[Any]:
    missing = [k for k in keys if p.get(k) is None]
    if missing:
        raise EditError(f"missing field(s): {', '.join(missing)}")
    return [p[k] for k in keys]


def _rel_id_list(rel_ids: Any, *, allow_empty: bool) -> list[int]:
    """Distinct int relationship ids (type-checked BEFORE hashing, so [[1]] is a 400, not a 500)."""
    if not isinstance(rel_ids, list) or any(not isinstance(r, int) or isinstance(r, bool) for r in rel_ids) \
            or len(set(rel_ids)) != len(rel_ids) or (not rel_ids and not allow_empty):
        raise EditError("rel_ids must be a " + ("" if allow_empty else "non-empty ") +
                        "list of distinct relationship ids")
    return rel_ids


def _entity_exists(con: duckdb.DuckDBPyConnection, eid: Any) -> int:
    if not isinstance(eid, int) or not con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [eid]).fetchone():
        raise EditError(f"no entity {eid!r}")
    return eid


def _edge(con: duckdb.DuckDBPyConnection, rel_id: Any) -> dict[str, Any]:
    row = con.execute("SELECT entity_a_id, entity_b_id, rel_type, status, catalyst_id FROM relationships WHERE rel_id = ?",
                      [rel_id]).fetchone()
    if not row:
        raise EditError(f"no relationship {rel_id!r}")
    return {"entity_a_id": row[0], "entity_b_id": row[1], "rel_type": row[2], "status": row[3], "catalyst_id": row[4]}


def _check_expect(current: dict[str, Any], expect: dict[str, Any] | None, keys: list[str]) -> None:
    expect = expect or {}
    stale = {k: (expect.get(k), current[k]) for k in keys if k in expect and expect[k] != current[k]}
    missing = [k for k in keys if k not in expect]
    if missing:
        raise EditError(f"expect must include current {', '.join(missing)} (what the admin saw)")
    if stale:
        raise EditConflict(f"changed since loaded: {stale}")


def _card_ids(con: duckdb.DuckDBPyConnection, catalyst_ids: set[int]) -> list[str]:
    if not catalyst_ids:
        return []
    marks = ",".join("?" * len(catalyst_ids))
    return [r[0] for r in con.execute(f"SELECT entry_id FROM catalysts WHERE catalyst_id IN ({marks})",
                                      list(catalyst_ids)).fetchall()]


def _apply(con: duckdb.DuckDBPyConnection, op: str, p: dict[str, Any], vocab: set[str]) -> tuple[dict, dict, bool, set[int]]:
    """Returns (before, after, revertible, touched catalyst ids)."""
    if op == "repoint_edge":
        (rel_id,) = _need(p, "rel_id")
        new = {k: p[k] for k in ("entity_a_id", "entity_b_id", "rel_type") if p.get(k) is not None}
        if not new:
            raise EditError("nothing to change: give entity_a_id, entity_b_id and/or rel_type")
        cur = _edge(con, rel_id)
        _check_expect(cur, p.get("expect"), list(new))
        for k in ("entity_a_id", "entity_b_id"):
            if k in new:
                _entity_exists(con, new[k])
        if "rel_type" in new and new["rel_type"] not in vocab:
            raise EditError(f"unknown rel_type {new['rel_type']!r}")
        res = {k: new.get(k, cur[k]) for k in ("entity_a_id", "rel_type", "entity_b_id")}
        if res["entity_a_id"] == res["entity_b_id"]:
            raise EditError("an edge can't point an entity at itself")
        # Refuse to create an exact copy of another edge on the same card: the
        # dedupe would silently delete one and a revert couldn't restore it.
        if con.execute("""SELECT 1 FROM relationships WHERE catalyst_id = ? AND rel_id <> ? AND entity_a_id = ?
                          AND rel_type = ? AND entity_b_id = ? AND status IN ('active', 'materialized')""",
                       [cur["catalyst_id"], rel_id, res["entity_a_id"], res["rel_type"], res["entity_b_id"]]).fetchone():
            raise EditError("that edge already exists on this card — set this one 'rejected' instead of repointing")
        sets = ", ".join(f"{k} = ?" for k in new)
        con.execute(f"UPDATE relationships SET {sets} WHERE rel_id = ?", [*new.values(), rel_id])
        return {"rel_id": rel_id, **{k: cur[k] for k in new}}, {"rel_id": rel_id, **new}, True, {cur["catalyst_id"]}

    if op == "set_edge_status":
        rel_id, status = _need(p, "rel_id", "status")
        if status not in EDGE_STATUSES:
            raise EditError(f"status must be one of {EDGE_STATUSES}")
        cur = _edge(con, rel_id)
        _check_expect(cur, p.get("expect"), ["status"])
        loop = cur["entity_a_id"] == cur["entity_b_id"]
        if status == "internal" and not loop:
            raise EditError("only a self-loop (same entity both ends) can be an internal event")
        if status in LIVE and loop:
            raise EditError("a self-loop can't be a live edge — use status 'internal' (internal event)")
        if status == "internal" and cur["status"] != "internal" and con.execute(
                """SELECT 1 FROM relationships WHERE catalyst_id = ? AND rel_id <> ? AND entity_a_id = ?
                   AND rel_type = ? AND entity_b_id = ? AND status = 'internal'""",
                [cur["catalyst_id"], rel_id, cur["entity_a_id"], cur["rel_type"], cur["entity_b_id"]]).fetchone():
            raise EditError("this internal event is already on the card")
        # Reviving a dead edge must not make it a live twin of another edge on the card.
        if status in LIVE and cur["status"] not in LIVE and con.execute(
                """SELECT 1 FROM relationships WHERE catalyst_id = ? AND rel_id <> ? AND entity_a_id = ?
                   AND rel_type = ? AND entity_b_id = ? AND status IN ('active', 'materialized')""",
                [cur["catalyst_id"], rel_id, cur["entity_a_id"], cur["rel_type"], cur["entity_b_id"]]).fetchone():
            raise EditError("an identical live edge already exists on this card")
        con.execute("UPDATE relationships SET status = ? WHERE rel_id = ?", [status, rel_id])
        return {"rel_id": rel_id, "status": cur["status"]}, {"rel_id": rel_id, "status": status}, True, {cur["catalyst_id"]}

    if op == "move_alias":   # to_entity_id None = drop
        alias, from_id = _need(p, "alias", "from_entity_id")
        to_id = p.get("to_entity_id")
        if not con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?", [from_id, alias]).fetchone():
            raise EditConflict(f"alias {alias!r} is no longer on entity {from_id}")
        to_had = False
        if to_id is not None:
            _entity_exists(con, to_id)
            if to_id == from_id:
                raise EditError("alias is already on that entity")
            to_had = bool(con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?",
                                      [to_id, alias]).fetchone())
            if not to_had:
                con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin')", [to_id, alias])
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?", [from_id, alias])
        # to_had: the target already carried this alias — a revert must not remove it there.
        return ({"alias": alias, "entity_id": from_id}, {"alias": alias, "entity_id": to_id, "to_had": to_had},
                True, set())

    if op == "create_entity":
        name, typ = _need(p, "name", "type")
        name = name.strip()
        if not name or con.execute("SELECT 1 FROM entities WHERE lower(name) = lower(?)", [name]).fetchone():
            raise EditError(f"entity {name!r} already exists or is empty")
        eid = con.execute("INSERT INTO entities (name, ticker, type) VALUES (?, ?, ?) RETURNING entity_id",
                          [name, (p.get("ticker") or None), typ]).fetchone()[0]
        return {}, {"entity_id": eid, "name": name}, True, set()

    if op == "spin_out":
        # An alias that actually names another organization ("SEER Robotics" on
        # Serve Robotics) becomes its own entity, taking the chosen edges along.
        alias, from_id, typ = _need(p, "alias", "from_entity_id", "type")
        _entity_exists(con, from_id)
        if not con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?", [from_id, alias]).fetchone():
            raise EditConflict(f"alias {alias!r} is no longer on entity {from_id}")
        name = (p.get("name") or alias).strip()
        if not name or con.execute("SELECT 1 FROM entities WHERE lower(name) = lower(?)", [name]).fetchone():
            raise EditError(f"entity {name!r} already exists or is empty — move the alias and repoint the edges instead")
        rel_ids = _rel_id_list(p.get("rel_ids") or [], allow_empty=True)
        moves, touched = [], set()
        for rel_id in rel_ids:
            cur = _edge(con, rel_id)
            sides = [k for k in ("entity_a_id", "entity_b_id") if cur[k] == from_id]
            if not sides:
                raise EditConflict(f"relationship {rel_id} no longer involves entity {from_id}")
            moves.append({"rel_id": rel_id, "sides": sides})
            touched.add(cur["catalyst_id"])
        if typ not in ENTITY_TYPES:
            raise EditError(f"type must be one of {ENTITY_TYPES}")
        parent = p.get("parent_id")
        if parent is not None:
            _entity_exists(con, parent)
        eid = con.execute("INSERT INTO entities (name, ticker, type) VALUES (?, ?, ?) RETURNING entity_id",
                          [name, (p.get("ticker") or None), typ]).fetchone()[0]
        if alias.lower() != name.lower():
            con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin')", [eid, alias])
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?", [from_id, alias])
        for m in moves:
            for side in m["sides"]:
                con.execute(f"UPDATE relationships SET {side} = ? WHERE rel_id = ?", [eid, m["rel_id"]])
        if parent is not None:   # "child of" the entity it was spun out of (or another one)
            entities.link(con, eid, parent, "subsidiary_of", confidence=None, reason="admin spin-out", source="admin")
        return ({"alias": alias, "entity_id": from_id, "moves": moves},
                {"entity_id": eid, "name": name, "alias_kept": alias.lower() != name.lower(), "parent_id": parent},
                True, touched)

    if op == "edit_entity":   # rename and/or retype; old name kept as an alias (matching memory)
        eid, expect = _need(p, "entity_id", "expect")
        _entity_exists(con, eid)
        name0, typ0 = con.execute("SELECT name, type FROM entities WHERE entity_id = ?", [eid]).fetchone()
        _check_expect({"name": name0, "type": typ0}, expect, ["name", "type"])
        name, typ = (p.get("name") or name0).strip(), p.get("type") or typ0
        if typ not in ENTITY_TYPES:
            raise EditError(f"type must be one of {ENTITY_TYPES}")
        if not name:
            raise EditError("name can't be empty")
        if name == name0 and typ == typ0:
            raise EditError("nothing to change")
        if name.lower() != name0.lower() and con.execute(
                "SELECT 1 FROM entities WHERE lower(name) = lower(?) AND entity_id <> ?", [name, eid]).fetchone():
            raise EditError(f"entity {name!r} already exists — merge instead")
        alias_added = False
        if name != name0:
            _rename_entity(con, eid, name)
            if p.get("keep_old_name", True) and name.lower() != name0.lower() and not con.execute(
                    "SELECT 1 FROM entity_aliases WHERE entity_id = ? AND lower(alias) = lower(?)", [eid, name0]).fetchone():
                con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin')", [eid, name0])
                alias_added = True
            # Social handles are keyed by the lowercased name: carry them over.
            con.execute("""INSERT INTO entity_handles (name_key, channel, handle, confidence, source, comment)
                           SELECT lower(trim(?)), channel, handle, confidence, source, comment FROM entity_handles
                           WHERE name_key = lower(trim(?)) ON CONFLICT DO NOTHING""", [name, name0])
        if typ != typ0:
            con.execute("UPDATE entities SET type = ? WHERE entity_id = ?", [typ, eid])
        # Card docs carry entity name + type and hashtags are derived from both
        # (hashtags.py tags by type): any change re-tags + refreshes its cards.
        touched = _live_catalysts(con, eid)
        # rel_subtype reads names + source type: refresh EVERY row of the entity (rejected / invalidated
        # rows too — they can be restored); live cards also refresh via store_for_catalyst.
        subtypes.refresh(con, _any_catalysts(con, eid))
        return ({"entity_id": eid, "name": name0, "type": typ0},
                {"entity_id": eid, "name": name, "type": typ, "alias_added": alias_added}, True, touched)

    if op == "delete_entity":   # only an entity no relationship row points at
        eid, expect_name = _need(p, "entity_id", "expect_name")
        _entity_exists(con, eid)
        name, ticker, typ = con.execute("SELECT name, ticker, type FROM entities WHERE entity_id = ?", [eid]).fetchone()
        if name != expect_name:
            raise EditConflict(f"entity {eid} is now named {name!r}")
        n = con.execute("SELECT COUNT(*) FROM relationships WHERE ? IN (entity_a_id, entity_b_id)", [eid]).fetchone()[0]
        edges: list[dict[str, Any]] = []
        if p.get("with_edges"):
            try:
                expect_edges = int(p.get("expect_edges"))
            except (TypeError, ValueError):
                raise EditError("with_edges needs expect_edges (how many relationships you saw)") from None
        if n and not p.get("with_edges"):
            raise EditError(f"{name!r} still has {n} relationship row(s) (rejected ones count) — repoint them first, "
                            "spin it back in, or delete it together with its edges")
        if n:   # a concept / fake entity: its edges go with it (hard delete, rows kept for the revert)
            edges = _rel_rows(con, "? IN (entity_a_id, entity_b_id)", [eid])
            shown = sum(1 for e in edges if e["entity_a_id"] != e["entity_b_id"])   # the editor doesn't list self-loops
            if expect_edges != shown:
                raise EditConflict(f"{name!r} now has {shown} relationships, not {expect_edges} — reload")
            con.execute("DELETE FROM relationships WHERE ? IN (entity_a_id, entity_b_id)", [eid])
        aliases = [a for (a,) in con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?", [eid]).fetchall()]
        links = [list(r) for r in con.execute("""SELECT child_id, parent_id, relation, confidence, reason, source
                                                 FROM entity_links WHERE ? IN (child_id, parent_id)""", [eid]).fetchall()]
        unresolved = [[m, c, t.isoformat() if t is not None else None] for m, c, t in con.execute(
            "SELECT mention, catalyst_id, created_at FROM unresolved_entities WHERE resolved_to = ?", [eid]).fetchall()]
        con.execute("UPDATE unresolved_entities SET resolved_to = NULL WHERE resolved_to = ?", [eid])
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ?", [eid])
        con.execute("DELETE FROM entity_links WHERE ? IN (child_id, parent_id)", [eid])
        con.execute("DELETE FROM entities WHERE entity_id = ?", [eid])
        return ({"entity_id": eid, "name": name, "ticker": ticker, "type": typ, "aliases": aliases, "links": links,
                 "unresolved": unresolved, "edges": edges}, {"deleted": eid, "edges_deleted": len(edges)}, True,
                {e["catalyst_id"] for e in edges})

    if op == "delete_edge":   # hard delete of one relationship row (reject = soft); row kept for the revert
        (rel_id,) = _need(p, "rel_id")
        cur = _edge(con, rel_id)
        _check_expect(cur, p.get("expect"), ["status"])
        rows = _rel_rows(con, "rel_id = ?", [rel_id])
        con.execute("DELETE FROM relationships WHERE rel_id = ?", [rel_id])
        return {"rel_id": rel_id, "row": rows[0]}, {"deleted": rel_id}, True, {cur["catalyst_id"]}

    if op == "add_alias":
        eid, alias = _need(p, "entity_id", "alias")
        _entity_exists(con, eid)
        alias = str(alias).strip()
        if len(alias) < 2:
            raise EditError("alias is empty")
        from .names import case_key as _ck  # noqa: PLC0415

        def case_key(x: str) -> str:      # case, punctuation, spacing AND dots inside a word: "A.W.S." == "AWS"
            return _ck(x).replace(".", "")

        own = [n for (n,) in con.execute("SELECT name FROM entities WHERE entity_id = ? UNION ALL "
                                         "SELECT alias FROM entity_aliases WHERE entity_id = ?", [eid, eid]).fetchall()]
        if any(case_key(n) == case_key(alias) for n in own):
            raise EditError(f"{alias!r} is already this entity's name or alias")
        key = case_key(alias)    # same comparison as the own-name check: "A.W.S." is "AWS"
        other = next((n for i, n, label in con.execute(
            """SELECT entity_id, name, name FROM entities WHERE entity_id <> ?
               UNION ALL SELECT a.entity_id, e.name, a.alias FROM entity_aliases a JOIN entities e USING (entity_id)
               WHERE a.entity_id <> ?""", [eid, eid]).fetchall() if case_key(label) == key), None)
        if other:
            raise EditError(f"{alias!r} already names {other!r} — move that alias or merge instead")
        con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin')",
                    [eid, alias])
        return {"entity_id": eid, "alias": None}, {"entity_id": eid, "alias": alias}, True, set()

    if op in ("link", "unlink"):
        child, parent, relation = _need(p, "child_id", "parent_id", "relation")
        if relation not in LINK_RELATIONS:
            raise EditError(f"relation must be one of {LINK_RELATIONS}")
        _entity_exists(con, child), _entity_exists(con, parent)
        if relation == "sister_of" and child > parent:
            child, parent = parent, child
        exists = bool(con.execute("SELECT 1 FROM entity_links WHERE child_id = ? AND parent_id = ? AND relation = ?",
                                  [child, parent, relation]).fetchone())
        key = {"child_id": child, "parent_id": parent, "relation": relation}
        if op == "link":
            if exists:
                raise EditError("link already exists")
            entities.link(con, child, parent, relation, confidence=None, reason="admin", source="admin")
        else:
            if not exists:
                raise EditConflict("link no longer exists")
            con.execute("DELETE FROM entity_links WHERE child_id = ? AND parent_id = ? AND relation = ?",
                        [child, parent, relation])
        return {**key, "exists": exists}, {**key, "exists": op == "link"}, True, set()

    if op == "merge":   # not revertible — entities.merge folds aliases/links/handles and deletes src
        src, dst = _need(p, "src_id", "dst_id")
        _entity_exists(con, src), _entity_exists(con, dst)
        if src == dst:
            raise EditError("can't merge an entity into itself")
        touched = {r[0] for r in con.execute(
            "SELECT DISTINCT catalyst_id FROM relationships WHERE ? IN (entity_a_id, entity_b_id)", [src]).fetchall()}
        names = dict(con.execute("SELECT entity_id, name FROM entities WHERE entity_id IN (?, ?)", [src, dst]).fetchall())
        merged = entities.merge(con, src, dst, confidence=None, reason="admin", source="admin")
        return ({"src": names[src], "dst": names[dst]},
                {"merged_into": names[dst], "duplicate_rows_removed": merged.get("deduped", 0)}, False, touched)

    raise EditError(f"unknown op {op!r}")


def apply_edit(con: duckdb.DuckDBPyConnection, op: str, payload: dict[str, Any], *, email: str, ip: str | None,
               vocab: set[str], reverts: str | None = None) -> dict[str, Any]:
    """Validate, apply and audit one edit. Returns {edit_id, before, after, cards}."""
    ensure_schema(con)
    before, after, revertible, touched = _apply(con, op, payload, vocab)
    if touched:
        direction.dedupe_rows(con, sorted(touched))
        for cid in touched:
            hashtags.store_for_catalyst(con, cid)
    edit_id = uuid.uuid4().hex[:12]
    con.execute("""INSERT INTO admin_edits (edit_id, email, ip, op, payload, before, after, revertible, reverts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [edit_id, email, ip, op, json.dumps(payload), json.dumps(before), json.dumps(after), revertible, reverts])
    if reverts:
        con.execute("UPDATE admin_edits SET reverted_by = ? WHERE edit_id = ?", [edit_id, reverts])
    return {"edit_id": edit_id, "op": op, "before": before, "after": after, "revertible": revertible,
            "cards": _card_ids(con, touched)}


def repoint_many(con: duckdb.DuckDBPyConnection, *, from_entity_id: int, to_entity_id: int, rel_ids: list[int],
                 email: str, ip: str | None, vocab: set[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bulk repoint: every listed edge's side(s) that equal from_entity_id move
    to to_entity_id. Each edge goes through apply_edit("repoint_edge") — same
    guards (expect, self-loop, live-twin), same dedupe/hashtags, one audit row
    each (individually revertible). All-or-nothing: any refusal raises and the
    caller's working copy is discarded, so nothing is pushed."""
    _entity_exists(con, from_entity_id)
    _entity_exists(con, to_entity_id)
    if from_entity_id == to_entity_id:
        raise EditError("source and target entity are the same")
    _rel_id_list(rel_ids, allow_empty=False)
    done, cards = [], set()
    for rel_id in rel_ids:
        cur = _edge(con, rel_id)
        sides = [k for k in ("entity_a_id", "entity_b_id") if cur[k] == from_entity_id]
        if not sides:
            raise EditConflict(f"relationship {rel_id} no longer involves entity {from_entity_id}")
        payload = {"rel_id": rel_id, **{k: to_entity_id for k in sides},
                   "expect": {k: from_entity_id for k in sides}, **(extra or {})}
        try:
            out = apply_edit(con, "repoint_edge", payload, email=email, ip=ip, vocab=vocab)
        except (EditError, EditConflict) as exc:
            raise type(exc)(f"relationship {rel_id}: {exc} — nothing was saved") from exc
        done.append(out["edit_id"])
        cards.update(out["cards"])
    return {"edit_id": ",".join(done), "edit_ids": done, "op": "repoint_many", "count": len(done),
            "cards": sorted(cards)}


def spin_out_many(con: duckdb.DuckDBPyConnection, *, from_entity_id: int, aliases: list[str], type_: str,
                  make_parent: bool, email: str, ip: str | None, vocab: set[str],
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Each alias → its own entity (name = alias), carrying the live edges that
    reached from_entity ONLY through that alias (computed here, at save time,
    from the card text — same rule as the alias pre-tick); optionally a child
    of from_entity. Each goes through apply_edit("spin_out") — same guards,
    one revertible audit row each. All-or-nothing via the caller's _write."""
    _entity_exists(con, from_entity_id)
    if not isinstance(aliases, list) or not aliases or len({a.lower() for a in aliases if isinstance(a, str)}) != len(aliases):
        raise EditError("aliases must be a non-empty list of distinct alias strings")
    name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [from_entity_id]).fetchone()
    done, cards = [], set()
    for alias in aliases:
        rel_ids = _via(_live_rels_with_text(con, from_entity_id), name, alias)   # after earlier spins in this batch
        payload = {"alias": alias, "from_entity_id": from_entity_id, "type": type_, "rel_ids": rel_ids,
                   **({"parent_id": from_entity_id} if make_parent else {}), **(extra or {})}
        try:
            out = apply_edit(con, "spin_out", payload, email=email, ip=ip, vocab=vocab)
        except (EditError, EditConflict) as exc:
            raise type(exc)(f"alias {alias!r}: {exc} — nothing was saved") from exc
        done.append(out["edit_id"])
        cards.update(out["cards"])
    return {"edit_id": ",".join(done), "edit_ids": done, "op": "spin_out_many", "count": len(done),
            "cards": sorted(cards)}


def spin_in_many(con: duckdb.DuckDBPyConnection, *, into_entity_id: int, child_ids: list[int], email: str,
                 ip: str | None, vocab: set[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Each listed CHILD of into_entity folds back into it (its name becomes an
    alias, its edges move over) — a merge, so one-way. Only entities recorded
    as subsidiary_of into_entity qualify."""
    _entity_exists(con, into_entity_id)
    child_ids = _rel_id_list(child_ids, allow_empty=False)
    for c in child_ids:
        if not con.execute("SELECT 1 FROM entity_links WHERE child_id = ? AND parent_id = ? AND relation = 'subsidiary_of'",
                           [c, into_entity_id]).fetchone():
            raise EditError(f"entity {c} is not a child of {into_entity_id}")
    done, cards = [], set()
    for c in child_ids:
        out = apply_edit(con, "merge", {"src_id": c, "dst_id": into_entity_id, **(extra or {})},
                         email=email, ip=ip, vocab=vocab)
        done.append(out["edit_id"])
        cards.update(out["cards"])
    return {"edit_id": ",".join(done), "edit_ids": done, "op": "spin_in_many", "count": len(done),
            "cards": sorted(cards)}


def revert_edit(con: duckdb.DuckDBPyConnection, edit_id: str, *, email: str, ip: str | None,
                vocab: set[str]) -> dict[str, Any]:
    """Apply the inverse of a recorded edit (guarded by its `after` state)."""
    ensure_schema(con)
    row = con.execute("SELECT op, before, after, revertible, reverted_by FROM admin_edits WHERE edit_id = ?",
                      [edit_id]).fetchone()
    if not row:
        raise EditError(f"no edit {edit_id!r}")
    op, before, after, revertible, reverted_by = row[0], json.loads(row[1]), json.loads(row[2]), row[3], row[4]
    if not revertible:
        raise EditError(f"{op} edits can't be reverted automatically")
    if reverted_by:
        raise EditError(f"already reverted by {reverted_by}")
    if op == "repoint_edge":
        keys = [k for k in before if k != "rel_id"]
        inverse = ("repoint_edge", {"rel_id": before["rel_id"], **{k: before[k] for k in keys},
                                    "expect": {k: after[k] for k in keys}})
    elif op == "set_edge_status":
        inverse = ("set_edge_status", {"rel_id": before["rel_id"], "status": before["status"],
                                       "expect": {"status": after["status"]}})
    elif op == "move_alias":
        alias, orig, target = before["alias"], before["entity_id"], after["entity_id"]
        if target is not None and not after.get("to_had") and not con.execute(
                "SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?", [target, alias]).fetchone():
            raise EditConflict(f"alias {alias!r} is no longer on entity {target}")
        con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin') "
                    "ON CONFLICT DO NOTHING", [orig, alias])
        if target is not None and not after.get("to_had"):
            con.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?", [target, alias])
        return _record_manual_revert(con, edit_id, email, ip, before, after)
    elif op == "create_entity":
        eid = after["entity_id"]
        used = con.execute("""SELECT (SELECT COUNT(*) FROM relationships WHERE ? IN (entity_a_id, entity_b_id))
                                   + (SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ?)
                                   + (SELECT COUNT(*) FROM entity_links WHERE ? IN (child_id, parent_id))""",
                           [eid, eid, eid]).fetchone()[0]
        if used:
            raise EditConflict(f"entity {after['name']!r} is in use ({used} references) — unlink/move them first")
        con.execute("DELETE FROM entities WHERE entity_id = ?", [eid])
        return _record_manual_revert(con, edit_id, email, ip, after, {})
    elif op == "spin_out":
        eid, orig, alias = after["entity_id"], before["entity_id"], before["alias"]
        if not con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [eid]).fetchone():
            raise EditConflict(f"entity {after['name']!r} no longer exists (merged?) — revert by hand")
        moved = {m["rel_id"] for m in before["moves"]}
        for m in before["moves"]:
            cur = _edge(con, m["rel_id"])
            if any(cur[side] != eid for side in m["sides"]):
                raise EditConflict(f"relationship {m['rel_id']} was changed since the spin-out")
        parent = after.get("parent_id")   # the spin-out's own parent link doesn't count as "other"
        others = con.execute("""SELECT (SELECT COUNT(*) FROM relationships WHERE ? IN (entity_a_id, entity_b_id)
                                        AND rel_id NOT IN (SELECT UNNEST(?::BIGINT[])))
                                     + (SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ? AND alias <> ?)
                                     + (SELECT COUNT(*) FROM entity_links WHERE ? IN (child_id, parent_id)
                                        AND NOT (child_id = ? AND parent_id = ? AND relation = 'subsidiary_of'))""",
                             [eid, sorted(moved) or [0], eid, alias, eid, eid, parent if parent is not None else -1]
                             ).fetchone()[0]
        if others:
            raise EditConflict(f"{after['name']!r} gained {others} other reference(s) since — move them first")
        if after.get("alias_kept") and not con.execute(
                "SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?", [eid, alias]).fetchone():
            raise EditConflict(f"alias {alias!r} is no longer on {after['name']!r}")
        if con.execute("SELECT 1 FROM entity_aliases WHERE alias = ? AND entity_id NOT IN (?, ?)",
                       [alias, eid, orig]).fetchone():
            raise EditConflict(f"alias {alias!r} is now on another entity — sort that out first")
        # Same rule as repoint_edge: moving an edge back must not make it a copy
        # of a live edge on its card (dedupe would silently delete that one).
        for m in before["moves"]:
            cur = _edge(con, m["rel_id"])
            res = {k: (orig if k in m["sides"] else cur[k]) for k in ("entity_a_id", "entity_b_id")}
            if con.execute("""SELECT 1 FROM relationships WHERE catalyst_id = ? AND rel_id <> ? AND entity_a_id = ?
                              AND rel_type = ? AND entity_b_id = ? AND status IN ('active', 'materialized')""",
                           [cur["catalyst_id"], m["rel_id"], res["entity_a_id"], cur["rel_type"],
                            res["entity_b_id"]]).fetchone():
                raise EditConflict(f"relationship {m['rel_id']}: the same edge now exists on its card again — "
                                   "reject one of them first")
        touched = set()
        for m in before["moves"]:
            for side in m["sides"]:
                con.execute(f"UPDATE relationships SET {side} = ? WHERE rel_id = ?", [orig, m["rel_id"]])
            touched.add(_edge(con, m["rel_id"])["catalyst_id"])
        con.execute("DELETE FROM entity_links WHERE child_id = ?", [eid])   # only its own parent link is left
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ?", [eid])
        con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin') "
                    "ON CONFLICT DO NOTHING", [orig, alias])
        con.execute("DELETE FROM entities WHERE entity_id = ?", [eid])
        if touched:
            direction.dedupe_rows(con, sorted(touched))
            for cid in touched:
                hashtags.store_for_catalyst(con, cid)
        return _record_manual_revert(con, edit_id, email, ip, before, after, cards=_card_ids(con, touched))
    elif op == "edit_entity":
        eid = before["entity_id"]
        if not con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [eid]).fetchone():
            raise EditConflict(f"entity {eid} no longer exists")
        cur_name, cur_type = con.execute("SELECT name, type FROM entities WHERE entity_id = ?", [eid]).fetchone()
        if (cur_name, cur_type) != (after["name"], after["type"]):
            raise EditConflict(f"entity {eid} changed since (now {cur_name!r}, {cur_type})")
        if before["name"] != cur_name and con.execute(
                "SELECT 1 FROM entities WHERE lower(name) = lower(?) AND entity_id <> ?", [before["name"], eid]).fetchone():
            raise EditConflict(f"name {before['name']!r} is taken by another entity now")
        if after.get("alias_added"):
            con.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?", [eid, before["name"]])
        if before["name"] != cur_name:
            _rename_entity(con, eid, before["name"])
        if before["type"] != cur_type:
            con.execute("UPDATE entities SET type = ? WHERE entity_id = ?", [before["type"], eid])
        touched = _live_catalysts(con, eid)
        subtypes.refresh(con, _any_catalysts(con, eid))
        for cid in touched:
            hashtags.store_for_catalyst(con, cid)
        return _record_manual_revert(con, edit_id, email, ip, before, after, cards=_card_ids(con, touched))
    elif op == "delete_entity":
        eid = before["entity_id"]
        if con.execute("SELECT 1 FROM entities WHERE entity_id = ? OR lower(name) = lower(?)",
                       [eid, before["name"]]).fetchone():
            raise EditConflict(f"id {eid} or name {before['name']!r} is in use again — restore by hand")
        con.execute("INSERT INTO entities (entity_id, name, ticker, type) VALUES (?, ?, ?, ?)",
                    [eid, before["name"], before["ticker"], before["type"]])
        for a in before["aliases"]:
            con.execute("INSERT INTO entity_aliases (entity_id, alias, verified, source) VALUES (?, ?, true, 'admin') "
                    "ON CONFLICT DO NOTHING", [eid, a])
        skipped = []
        for child, parent, rel, conf, reason, source in before["links"]:
            if con.execute("SELECT 1 FROM entities WHERE entity_id = ?", [parent if child == eid else child]).fetchone():
                con.execute("""INSERT INTO entity_links (child_id, parent_id, relation, confidence, reason, source)
                               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""", [child, parent, rel, conf, reason, source])
            else:
                skipped.append([child, parent, rel])
        for mention, cid, created in before.get("unresolved", []):
            con.execute("""UPDATE unresolved_entities SET resolved_to = ? WHERE mention = ? AND catalyst_id = ?
                           AND created_at IS NOT DISTINCT FROM CAST(? AS TIMESTAMP) AND resolved_to IS NULL""",
                        [eid, mention, cid, created])
        touched = _restore_rel_rows(con, before.get("edges") or [])
        for cid in touched:
            hashtags.store_for_catalyst(con, cid)
        out = _record_manual_revert(con, edit_id, email, ip, before, after, cards=_card_ids(con, touched))
        out["links_skipped"] = skipped   # other end no longer exists
        return out
    elif op == "delete_edge":
        touched = _restore_rel_rows(con, [before["row"]])
        direction.dedupe_rows(con, sorted(touched))
        for cid in touched:
            hashtags.store_for_catalyst(con, cid)
        return _record_manual_revert(con, edit_id, email, ip, before, after, cards=_card_ids(con, touched))
    elif op == "add_alias":
        if not con.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?",
                           [after["entity_id"], after["alias"]]).fetchone():
            raise EditConflict(f"alias {after['alias']!r} is no longer on that entity")
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?", [after["entity_id"], after["alias"]])
        return _record_manual_revert(con, edit_id, email, ip, before, after)
    elif op in ("link", "unlink"):
        key = {k: before[k] for k in ("child_id", "parent_id", "relation")}
        inverse = ("unlink" if op == "link" else "link", key)
    else:
        raise EditError(f"no inverse for {op}")
    return apply_edit(con, inverse[0], inverse[1], email=email, ip=ip, vocab=vocab, reverts=edit_id)


def _record_manual_revert(con: duckdb.DuckDBPyConnection, edit_id: str, email: str, ip: str | None,
                          before: dict, after: dict, cards: list[str] | None = None) -> dict[str, Any]:
    new_id = uuid.uuid4().hex[:12]
    con.execute("""INSERT INTO admin_edits (edit_id, email, ip, op, payload, before, after, revertible, reverts)
                   VALUES (?, ?, ?, 'revert', ?, ?, ?, false, ?)""",
                [new_id, email, ip, json.dumps({"edit_id": edit_id}), json.dumps(after), json.dumps(before), edit_id])
    con.execute("UPDATE admin_edits SET reverted_by = ? WHERE edit_id = ?", [new_id, edit_id])
    return {"edit_id": new_id, "op": "revert", "before": after, "after": before, "revertible": False,
            "cards": cards or []}


# ── LLM suggestions (read-only) ────────────────────────────────────

_SUGGEST_PROMPT = """\
Each relationship below was attached to "{entity}". That entity belongs to a
corporate family; stories about another family member sometimes land on it
through a shared brand word (e.g. "Amazon" stored on Amazon Web Services).
For EACH relationship, read its story and mechanism and pick which candidate
the story actually refers to. Keep "{entity}" if it is right; pick another
candidate only when the story's own words point to it (e.g. a delivery story
→ the logistics unit, not the cloud unit). If unsure, keep "{entity}" with a
low confidence.

Candidates: {candidates}

{edges}
"""


def suggest_repoints(con: duckdb.DuckDBPyConnection, client: Any, cfg: Any, entity_id: int,
                     rel_ids: list[int]) -> list[dict[str, Any]]:
    """One LLM call per 20 edges → [{rel_id, side, current, suggested_id,
    suggested, confidence, reason}]. Writes nothing; the caller applies any
    accepted suggestion as an ordinary audited repoint_edge edit."""
    from pydantic import BaseModel, Field  # noqa: PLC0415

    from .extract import _gen_config  # noqa: PLC0415
    from .retry import call_with_retry  # noqa: PLC0415

    detail = entity_detail(con, entity_id)
    fam = {f["id"]: f["name"] for f in detail["family"]}
    parents = [f["id"] for f in detail["family"] if f["role"] == "parent"]
    for pid in parents:   # siblings under the same parent
        for (cid, cname) in con.execute("""SELECT l.child_id, e.name FROM entity_links l JOIN entities e
                                           ON e.entity_id = l.child_id WHERE l.parent_id = ? AND l.relation = 'subsidiary_of'""",
                                        [pid]).fetchall():
            fam[cid] = cname
    fam.pop(entity_id, None)
    candidates = {detail["name"]: entity_id, **{n: i for i, n in fam.items()}}
    edges = [r for r in detail["relationships"] if r["rel_id"] in set(rel_ids)]

    class Pick(BaseModel):
        rel_id: int
        entity: str = Field(description="Exactly one candidate name.")
        confidence: float = Field(ge=0, le=1)
        reason: str = Field(min_length=3, max_length=300)

    class Picks(BaseModel):
        picks: list[Pick]

    out = []
    for i in range(0, len(edges), 20):
        chunk = edges[i:i + 20]
        txt = "\n\n".join(
            f"[{r['rel_id']}] {r['a']['name']} --{r['rel_type']}--> {r['b']['name']}\n"
            f"story: {r['headline']}. {(r['finding'] or '')[:600]}\nmechanism: {r['mechanism'] or ''}" for r in chunk)
        prompt = _SUGGEST_PROMPT.format(entity=detail["name"], candidates="; ".join(candidates), edges=txt)
        model = cfg.extraction.model
        resp = call_with_retry(
            lambda: client.models.generate_content(
                model=model, contents=prompt,
                config=_gen_config(model, cfg.extraction.direction_thinking_level, temperature=0.0,
                                   max_output_tokens=16384, response_mime_type="application/json",
                                   response_schema=Picks)),
            op="admin_suggest", context={"edges": len(chunk)})
        parsed = resp.parsed
        if parsed is None:
            raise EditError(f"LLM returned no parseable suggestions ({(resp.text or '')[:150]!r})")
        by_id = {p.rel_id: p for p in parsed.picks}
        for r in chunk:
            p = by_id.get(r["rel_id"])
            side = "entity_a_id" if r["a"]["id"] == entity_id else "entity_b_id"
            sid = candidates.get(p.entity) if p else None
            out.append({"rel_id": r["rel_id"], "side": side, "current": detail["name"],
                        "suggested_id": sid if sid is not None else entity_id,
                        "suggested": p.entity if p and sid is not None else detail["name"],
                        "confidence": p.confidence if p else 0.0, "reason": p.reason if p else "no answer"})
    return out
