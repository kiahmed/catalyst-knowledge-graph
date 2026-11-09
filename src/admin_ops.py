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

from . import direction, entities, hashtags

LIVE = ("active", "materialized")
EDGE_STATUSES = ("active", "materialized", "invalidated", "rejected")   # every status a relationship can hold
LINK_RELATIONS = ("subsidiary_of", "sister_of")


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
    return {"id": eid, "name": name, "type": typ, "ticker": ticker, "aliases": aliases, "family": family,
            "relationships": rels}


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
                con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)", [to_id, alias])
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
        con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?) ON CONFLICT DO NOTHING", [orig, alias])
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
    elif op in ("link", "unlink"):
        key = {k: before[k] for k in ("child_id", "parent_id", "relation")}
        inverse = ("unlink" if op == "link" else "link", key)
    else:
        raise EditError(f"no inverse for {op}")
    return apply_edit(con, inverse[0], inverse[1], email=email, ip=ip, vocab=vocab, reverts=edit_id)


def _record_manual_revert(con: duckdb.DuckDBPyConnection, edit_id: str, email: str, ip: str | None,
                          before: dict, after: dict) -> dict[str, Any]:
    new_id = uuid.uuid4().hex[:12]
    con.execute("""INSERT INTO admin_edits (edit_id, email, ip, op, payload, before, after, revertible, reverts)
                   VALUES (?, ?, ?, 'revert', ?, ?, ?, false, ?)""",
                [new_id, email, ip, json.dumps({"edit_id": edit_id}), json.dumps(after), json.dumps(before), edit_id])
    con.execute("UPDATE admin_edits SET reverted_by = ? WHERE edit_id = ?", [new_id, edit_id])
    return {"edit_id": new_id, "op": "revert", "before": after, "after": before, "revertible": False, "cards": []}


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
