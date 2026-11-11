"""Entity identity + corporate structure, judged by the LLM (never guessed).

Three questions per group of similar-named entities, answered in one call
under a strict response schema:
  - same_as:   is this the SAME real-world entity as another member (name
               variant, abbreviation, legal suffix)? → merge into it.
  - parent:    is it owned/controlled by another member? → subsidiary_of link.
  - sister_of: does it share an owner with other members (owner not listed)?
  - wrong_aliases: aliases recorded for it that name something else.

Used by dev-utils/entity_audit.py (one-time cleanup + admin fixes). Merges
re-point every reference to the surviving entity, drop duplicate rows the
merge creates, recompute the affected cards' hashtags and log to
entity_merges. Links go to entity_links.
"""
from __future__ import annotations

import collections
import logging
import re
from typing import Any

import duckdb
from google import genai
from pydantic import BaseModel, Field

from . import direction, hashtags

log = logging.getLogger("robotics.entities")

_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|ltd|limited|llc|plc|ag|sa|se|gmbh|group|holdings|"
                     r"technologies|technology|robotics|ai|the|company|companies)\b\.?")
_STOP = {"and", "for", "of", "in", "on", "at", "de", "la", "le"}
_MAX_TOKEN_FREQ = 10   # a word shared by more names than this is generic, not a brand


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", _SUFFIX.sub("", re.sub(r"[^\w\s]", " ", name.lower()))).strip()


def candidate_groups(names: dict[int, str]) -> list[list[int]]:
    """Small groups of entities that MIGHT be related, for the LLM to judge:
    same name after stripping legal suffixes, or sharing a rare word that
    starts at least one member's name (a brand: "Hyundai", "Samsung").
    No transitive chaining — that merges unrelated names into giant blobs."""
    normed = {eid: _norm(n) for eid, n in names.items()}
    freq = collections.Counter(t for v in normed.values() for t in set(v.split()))
    by_tok: dict[str, set[int]] = collections.defaultdict(set)
    leads: set[str] = set()
    exact: dict[str, set[int]] = collections.defaultdict(set)
    for eid, v in normed.items():
        toks = v.split()
        exact[v].add(eid)
        if toks:
            leads.add(toks[0])
        for t in set(toks):
            if len(t) >= 3 and t not in _STOP and freq[t] <= _MAX_TOKEN_FREQ:
                by_tok[t].add(eid)
    groups = {frozenset(v) for k, v in exact.items() if k and len(v) > 1}
    groups |= {frozenset(v) for t, v in by_tok.items() if len(v) > 1 and t in leads}
    kept: list[frozenset[int]] = []
    for g in sorted(groups, key=len, reverse=True):   # drop groups contained in a bigger one
        if not any(g <= k for k in kept):
            kept.append(g)
    return [sorted(g) for g in kept]


class EntityVerdict(BaseModel):
    name: str = Field(description="Exactly as listed.")
    same_as: str | None = Field(
        default=None,
        description="Another member's exact name if this is the SAME real-world entity (spelling/"
                    "abbreviation/legal-suffix variant) — the one to KEEP (most complete official name). "
                    "Never for parent/subsidiary, products, divisions, or different firms sharing a word.")
    parent: str | None = Field(
        default=None, description="Another member's exact name that OWNS or CONTROLS this entity, if known.")
    sister_of: list[str] = Field(
        default_factory=list,
        description="Members that share an owner with this entity when that owner is NOT in the list.")
    wrong_aliases: list[str] = Field(
        default_factory=list, description="Aliases shown for this entity that actually name something else.")
    confidence: float = Field(ge=0, le=1, description="How sure you are of every claim above.")
    reason: str = Field(min_length=3, max_length=300)


class GroupVerdict(BaseModel):
    group: int
    entities: list[EntityVerdict]


class BatchVerdict(BaseModel):
    groups: list[GroupVerdict]


_PROMPT = """\
You maintain the company list of a robotics-industry knowledge graph. Each
group below holds entities whose names look alike. For EVERY entity decide,
using well-established public facts and the context shown:

1. same_as — the SAME real-world entity as another member (name variant,
   abbreviation, legal suffix, rebrand)? Give the member to KEEP. Distinct
   companies that share a word (Hyundai Rotem vs Hyundai Mobis), a parent and
   its subsidiary, a product, a division or a fund are NOT the same entity.
2. parent — owned or controlled by another member? (e.g. a subsidiary of a
   group listed here.) Only if you are confident it is true.
3. sister_of — shares an owner with other members when that owner is NOT listed.
4. wrong_aliases — aliases listed for it that actually refer to something else.

Generic categories ("Chinese robotics companies", "Traditional automation")
are not companies: never merge them with a company; leave them alone unless
two describe exactly the same category.

Only use names exactly as listed. If unsure, leave the field empty and give
a low confidence — a wrong merge corrupts the graph; a missed one is cheap.

Existing links already on record:
{links}

{groups}
"""


def _context(con: duckdb.DuckDBPyConnection, eid: int) -> str:
    name, typ, ticker = con.execute("SELECT name, type, ticker FROM entities WHERE entity_id = ?", [eid]).fetchone()
    aliases = [r[0] for r in con.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ? AND alias <> ? ORDER BY alias LIMIT 8",
        [eid, name]).fetchall()]
    n_rels, = con.execute("SELECT COUNT(*) FROM relationships WHERE ? IN (entity_a_id, entity_b_id) "
                          "AND status <> 'rejected'", [eid]).fetchone()
    heads = [r[0] for r in con.execute(
        """SELECT DISTINCT c.headline FROM relationships r JOIN catalysts c USING (catalyst_id)
           WHERE ? IN (r.entity_a_id, r.entity_b_id) AND r.status <> 'rejected' LIMIT 2""", [eid]).fetchall()]
    bits = [f"type={typ}"] + ([f"ticker={ticker}"] if ticker else []) + [f"{n_rels} relationships"]
    if aliases:
        bits.append("aliases=" + "; ".join(aliases))
    if heads:
        bits.append("seen in: " + " | ".join(h[:100] for h in heads))
    return f"- {name} ({', '.join(bits)})"


def existing_links(con: duckdb.DuckDBPyConnection, ids: list[int]) -> list[str]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [f"{a} --{rel}--> {b}" for a, rel, b in con.execute(f"""
        SELECT ea.name, l.relation, eb.name FROM entity_links l
        JOIN entities ea ON ea.entity_id = l.child_id JOIN entities eb ON eb.entity_id = l.parent_id
        WHERE l.child_id IN ({marks}) OR l.parent_id IN ({marks})""", ids + ids).fetchall()]


def judge(client: genai.Client, con: duckdb.DuckDBPyConnection, *, model: str, thinking_level: str,
          batch: list[list[int]]) -> dict[int, list[EntityVerdict]]:
    """One call for a batch of groups → {group index: verdicts}."""
    from .extract import _gen_config  # noqa: PLC0415
    from .retry import call_with_retry  # noqa: PLC0415

    ids = sorted({e for g in batch for e in g})
    groups_txt = "\n\n".join(f"## Group {gi}\n" + "\n".join(_context(con, e) for e in g)
                             for gi, g in enumerate(batch))
    prompt = _PROMPT.format(links="\n".join(existing_links(con, ids)) or "(none)", groups=groups_txt)
    resp = call_with_retry(
        lambda: client.models.generate_content(
            model=model, contents=prompt,
            config=_gen_config(model, thinking_level, temperature=0.0, max_output_tokens=32768,
                               response_mime_type="application/json", response_schema=BatchVerdict)),
        op="entity_judge", context={"groups": len(batch)})
    parsed: BatchVerdict | None = resp.parsed  # type: ignore[assignment]
    if parsed is None:
        raise RuntimeError(f"entity_judge: no parseable output ({(resp.text or '')[:200]!r})")
    return {g.group: g.entities for g in parsed.groups if 0 <= g.group < len(batch)}


# ── Alias audit ───────────────────────────────────────────────────

_ALIAS_SIMILAR = 80   # token_set_ratio at/above this = obviously the same name, not audited


def suspect_aliases(con: duckdb.DuckDBPyConnection, *, include_similar: bool = False) -> dict[int, list[str]]:
    """Aliases that don't look like their entity's name — where the old fuzzy
    tier (and LLM resolution slips) parked wrong mentions ("X" → Cognex).
    include_similar=True returns EVERY alias: token_set_ratio scores a name
    contained in a longer alias as 100 ("China" ⊂ "China Telecom") and rates
    look-alikes high ("SEER Robotics" ~ "Serve Robotics", "Samsung SDS" ~
    "Samsung SDI"), so the filtered set missed those (found 2026-11-09)."""
    from thefuzz import fuzz  # noqa: PLC0415

    out: dict[int, list[str]] = {}
    for eid, name, alias in con.execute("""SELECT e.entity_id, e.name, a.alias FROM entity_aliases a
                                           JOIN entities e USING (entity_id) WHERE a.alias <> e.name
                                           ORDER BY e.entity_id, a.alias""").fetchall():
        if include_similar or fuzz.token_set_ratio(name.lower(), alias.lower()) < _ALIAS_SIMILAR:
            out.setdefault(eid, []).append(alias)
    return out


class AliasVerdict(BaseModel):
    entity: str = Field(description="Entity name exactly as listed.")
    alias: str = Field(description="Alias exactly as listed.")
    keep: bool = Field(description="True only if the alias unambiguously names THIS entity.")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=3, max_length=300)


class AliasBatch(BaseModel):
    aliases: list[AliasVerdict]


_ALIAS_PROMPT = """Each entity below has recorded aliases: other spellings that, when they appear
in news, are resolved straight to that entity. A wrong alias silently attaches
another company's news to this one. For EVERY alias decide keep / drop:

keep — it unambiguously names THIS entity: legal/short name, abbreviation,
       ticker, former name, translation, or its own product/brand that no
       other organization uses.
drop — a generic description ("humanoid startups", "Chinese manufacturers"),
       a different organization (even a related or similarly named one), a
       person, a law, a program, a country/sector phrase, or anything ambiguous.

If unsure, drop with a moderate confidence — a missing alias only means the
next mention gets an LLM decision; a wrong one corrupts the graph.

{entities}
"""


def only_via_alias(text: str, name: str, alias: str) -> bool:
    """True when `text` names `alias` (whole words) but names `name` nowhere
    outside those alias mentions — "China Telecom ..." alone doesn't count as
    naming China; "Intel" isn't found inside "Physical Intelligence"."""
    import re  # noqa: PLC0415

    t = text.lower()
    # (?<!\w)/(?!\w), not \b: \b never matches after a trailing "." ("Inc.").
    a_re = re.compile(r"(?<!\w)" + re.escape(alias.lower()) + r"(?!\w)")
    if not a_re.search(t):
        return False
    n_re = re.compile(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)")
    if a_re.search(name.lower()) and n_re.search(t):
        return False          # alias inside the name ("Amazon" ⊂ "Amazon Web Services"): the name is there
    return not n_re.search(a_re.sub(" ", t))


def _alias_seen(con: duckdb.DuckDBPyConnection, eid: int, name: str, alias: str) -> str:
    """Card context for one alias: headlines of this entity's cards that name
    the alias but not the entity itself, i.e. stories that reached the entity
    only through this alias (SEER Robotics → Serve Robotics)."""
    heads = []
    for head, text in con.execute(
            """SELECT DISTINCT c.headline, lower(c.headline || ' ' || c.raw_finding) FROM relationships r
               JOIN catalysts c USING (catalyst_id) WHERE ? IN (r.entity_a_id, r.entity_b_id)
               AND r.status <> 'rejected' AND lower(c.headline || ' ' || c.raw_finding) LIKE ?""",
            [eid, f"%{alias.lower()}%"]).fetchall():
        if only_via_alias(text, name, alias):
            heads.append(head[:100])
            if len(heads) == 2:
                break
    return f"   (linked only through this alias in: {' | '.join(heads)})" if heads else ""


def judge_aliases(client: genai.Client, con: duckdb.DuckDBPyConnection, *, model: str, thinking_level: str,
                  batch: list[tuple[int, list[str]]]) -> list[AliasVerdict]:
    from .extract import _gen_config  # noqa: PLC0415
    from .retry import call_with_retry  # noqa: PLC0415

    blocks = []
    for eid, aliases in batch:
        name, typ, ticker = con.execute("SELECT name, type, ticker FROM entities WHERE entity_id = ?",
                                        [eid]).fetchone()
        heads = [r[0][:100] for r in con.execute(
            """SELECT DISTINCT c.headline FROM relationships r JOIN catalysts c USING (catalyst_id)
               WHERE ? IN (r.entity_a_id, r.entity_b_id) AND r.status <> 'rejected' LIMIT 2""", [eid]).fetchall()]
        head = f"- {name} (type={typ}" + (f", ticker={ticker}" if ticker else "") + \
            (f"; seen in: {' | '.join(heads)}" if heads else "") + ")"
        blocks.append(head + "\n" + "\n".join(f"    alias: {a}" + _alias_seen(con, eid, name, a) for a in aliases))
    prompt = _ALIAS_PROMPT.format(entities="\n".join(blocks))
    resp = call_with_retry(
        lambda: client.models.generate_content(
            model=model, contents=prompt,
            config=_gen_config(model, thinking_level, temperature=0.0, max_output_tokens=32768,
                               response_mime_type="application/json", response_schema=AliasBatch)),
        op="alias_judge", context={"entities": len(batch)})
    parsed: AliasBatch | None = resp.parsed  # type: ignore[assignment]
    if parsed is None:
        raise RuntimeError(f"alias_judge: no parseable output ({(resp.text or '')[:200]!r})")
    return parsed.aliases


# ── Applying decisions ─────────────────────────────────────────────


def merge(con: duckdb.DuckDBPyConnection, src_id: int, dst_id: int, *, confidence: float | None,
          reason: str, source: str) -> dict[str, int]:
    """Fold entity src into dst: relationships, aliases (incl. src's name),
    unresolved mentions, links and missing social handles move to dst; the
    cards it touched get duplicate rows dropped and hashtags recomputed."""
    if src_id == dst_id:
        return {}
    src_name, src_ticker, src_type = con.execute(
        "SELECT name, ticker, type FROM entities WHERE entity_id = ?", [src_id]).fetchone()
    dst_name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [dst_id]).fetchone()
    # Keep the source's ticker when the survivor has none ($TICKER tags on cards).
    con.execute("UPDATE entities SET ticker = COALESCE(ticker, ?) WHERE entity_id = ?", [src_ticker, dst_id])
    cards = [r[0] for r in con.execute(
        "SELECT DISTINCT catalyst_id FROM relationships WHERE ? IN (entity_a_id, entity_b_id)", [src_id]).fetchall()]
    for col in ("entity_a_id", "entity_b_id"):
        con.execute(f"UPDATE relationships SET {col} = ? WHERE {col} = ?", [dst_id, src_id])
    for alias in [src_name] + [r[0] for r in con.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", [src_id]).fetchall()]:
        con.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    [dst_id, alias])
    con.execute("DELETE FROM entity_aliases WHERE entity_id = ?", [src_id])
    con.execute("UPDATE unresolved_entities SET resolved_to = ? WHERE resolved_to = ?", [dst_id, src_id])
    con.execute("""INSERT INTO entity_links (child_id, parent_id, relation, confidence, reason, source)
                   SELECT ?, parent_id, relation, confidence, reason, source FROM entity_links
                   WHERE child_id = ? AND parent_id <> ? ON CONFLICT DO NOTHING""", [dst_id, src_id, dst_id])
    con.execute("""INSERT INTO entity_links (child_id, parent_id, relation, confidence, reason, source)
                   SELECT child_id, ?, relation, confidence, reason, source FROM entity_links
                   WHERE parent_id = ? AND child_id <> ? ON CONFLICT DO NOTHING""", [dst_id, src_id, dst_id])
    con.execute("DELETE FROM entity_links WHERE child_id = ? OR parent_id = ?", [src_id, src_id])
    # sister_of is stored lower-id-first; re-pointing can break that order.
    con.execute("""INSERT INTO entity_links (child_id, parent_id, relation, confidence, reason, source)
                   SELECT parent_id, child_id, relation, confidence, reason, source FROM entity_links
                   WHERE relation = 'sister_of' AND child_id > parent_id ON CONFLICT DO NOTHING""")
    con.execute("DELETE FROM entity_links WHERE relation = 'sister_of' AND child_id > parent_id")
    # Handles: keep dst's verified ones; a src verified handle replaces dst's
    # abstain/blocked row (handle NULL) for that channel.
    con.execute("""INSERT INTO entity_handles (name_key, channel, handle, confidence, source, comment)
                   SELECT lower(trim(?)), channel, handle, confidence, source, comment FROM entity_handles
                   WHERE name_key = lower(trim(?)) AND handle IS NOT NULL
                   ON CONFLICT (name_key, channel) DO UPDATE SET handle = excluded.handle,
                       confidence = excluded.confidence, source = excluded.source, comment = excluded.comment
                   WHERE entity_handles.handle IS NULL""",
                [dst_name, src_name])
    con.execute("DELETE FROM entities WHERE entity_id = ?", [src_id])
    # An edge between the two became a self-loop. A directional one is an internal event ("Tesla deploys
    # Optimus" after Optimus → Tesla): kept as status 'internal', outside every live count, graph and card
    # edge list (docs/ticket-internal-events.md). A two-party type ("X partners_with X") is noise: rejected.
    if cards:
        sym = sorted(direction.symmetric_types()) or [""]
        con.execute(f"""UPDATE relationships SET status = CASE WHEN rel_type IN (SELECT UNNEST(?::VARCHAR[]))
                            THEN 'rejected' ELSE 'internal' END
                        WHERE entity_a_id = ? AND entity_b_id = ? AND status IN ('active', 'materialized')
                        AND catalyst_id IN ({','.join('?' * len(cards))})""", [sym, dst_id, dst_id, *cards])
        # Two products merged into one company ("Tesla deploys Optimus", "Tesla deploys AI5") → one internal
        # event per (card, type): keep the most confident, drop the restatements (like dedupe_rows does).
        con.execute(f"""DELETE FROM relationships WHERE rel_id IN (
                          SELECT rel_id FROM (SELECT rel_id, ROW_NUMBER() OVER (PARTITION BY catalyst_id, rel_type
                                              ORDER BY confidence DESC, rel_id) AS rn
                                              FROM relationships WHERE entity_a_id = ? AND entity_b_id = ?
                                              AND status = 'internal'
                                              AND catalyst_id IN ({','.join('?' * len(cards))}))
                          WHERE rn > 1)""", [dst_id, dst_id, *cards])
    deduped = direction.dedupe_rows(con, cards)
    for cid in cards:
        hashtags.store_for_catalyst(con, cid)
    con.execute("""INSERT INTO entity_merges (src_id, dst_id, src_name, dst_name, src_ticker, src_type,
                                              confidence, reason, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [src_id, dst_id, src_name, dst_name, src_ticker, src_type, confidence, reason, source])
    return {"cards": len(cards), "deduped": deduped}


def link(con: duckdb.DuckDBPyConnection, child_id: int, parent_id: int, relation: str, *,
         confidence: float | None, reason: str, source: str) -> bool:
    """Record child --relation--> parent. sister_of is stored once (lower id first)."""
    if child_id == parent_id:
        return False
    if relation == "sister_of" and child_id > parent_id:
        child_id, parent_id = parent_id, child_id
    con.execute("""INSERT INTO entity_links (child_id, parent_id, relation, confidence, reason, source)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                [child_id, parent_id, relation, confidence, reason, source])
    return True


def resolve_merges(pairs: dict[int, int]) -> dict[int, int]:
    """src → final dst, following chains (A→B, B→C ⇒ A→C). A cycle (A→B→A)
    settles on its smallest id, so every member ends up on the same entity."""
    out: dict[int, int] = {}
    for src in pairs:
        seen, cur = [src], pairs[src]
        while cur in pairs and cur not in seen:
            seen.append(cur)
            cur = pairs[cur]
        if cur in seen:          # cycle: settle on the smallest id in it
            cur = min(seen[seen.index(cur):])
        if cur != src:
            out[src] = cur
    return out
