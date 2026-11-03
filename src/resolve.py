"""Entity resolver: exact match, else an LLM decision (never a spelling guess).

Tier 1 — exact match on entities.name or entity_aliases.alias (case-insensitive).
Tier 2 — LLM decision (decide_entities, one call per card, strict schema): given
         the card text and the closest existing entities (with aliases and
         corporate family), each non-exact name is either the SAME as one
         candidate (→ reuse + alias) or NEW (optionally subsidiary/sister of a
         candidate → entity_links). Replaces the old fuzzy-ratio tier, whose
         look-alike matches produced aliases like "NIO" → European Union.
Tier 3 — new entity, logged in unresolved_entities. Used when the LLM says
         new, is unsure (below cfg.resolution.min_confidence; also flagged for
         admin review) or fails (fail-safe: never a silent merge).

All tiers return an entity_id, so relationships can always be written.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import duckdb
from pydantic import BaseModel, Field
from thefuzz import fuzz, process as fuzz_process

from . import db
from .config import RoboticsConfig

log = logging.getLogger("robotics.resolve")

_CANDIDATES = 6       # closest existing names shown per new name
_MIN_SCORE = 60       # token_set_ratio floor for a name to be a candidate at all
_FAMILY = 10          # cap on corporate-family members added as extra candidates


@dataclass
class ResolveResult:
    entity_id: int
    tier: int           # 1 = exact, 2 = LLM same-as, 3 = new/unresolved
    matched_name: str   # canonical name the mention resolved to


@dataclass
class Decision:
    """The LLM's call for one non-exact name (keyed by the extracted name)."""
    same_as: int | None = None                    # existing entity_id to reuse
    links: list[tuple[int, str]] = field(default_factory=list)   # (other id, 'subsidiary_of'|'sister_of')
    confidence: float = 0.0
    reason: str = ""
    review: list[str] = field(default_factory=list)              # unsure claims → admin entry


class NameDecision(BaseModel):
    name: str = Field(description="The new name exactly as listed.")
    same_as: str | None = Field(
        default=None,
        description="A candidate's exact name if this IS that entity (spelling/abbreviation/legal-suffix "
                    "variant, rebrand). Never a parent, subsidiary, product, fund or a different firm.")
    parent: str | None = Field(default=None, description="A candidate that owns/controls it, if it is new.")
    sister_of: str | None = Field(default=None, description="A candidate sharing an owner not listed, if it is new.")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=3, max_length=300)


class NameDecisions(BaseModel):
    names: list[NameDecision]


_PROMPT = """\
You keep the company list of a robotics knowledge graph consistent. A news
item mentions the names below that do not exactly match an entity we already
have. For EACH, decide from the item and well-established public facts:

- same_as: it IS one of its candidates (name variant, abbreviation, legal
  suffix, rebrand) — give that candidate's exact name. A different company
  that shares a word (Hyundai Rotem vs Hyundai Motor Group), a parent or
  subsidiary, a product, a fund or a person is NOT the same entity.
- otherwise it is new; if a candidate owns/controls it give parent, or if they
  share an owner that is not listed give sister_of.

If unsure, leave fields empty with a low confidence: a wrong merge corrupts
the graph; a duplicate is cheap to fix later.

## News item
{text}

## Names to place, with their closest existing entities
{names}
"""


def _candidates(con: duckdb.DuckDBPyConnection, name: str) -> list[tuple[int, str]]:
    """Closest existing entities by name or alias, plus their corporate family."""
    pool = con.execute("""SELECT entity_id, name FROM entities
                          UNION ALL SELECT entity_id, alias FROM entity_aliases""").fetchall()
    hits = fuzz_process.extract(name, [n for _, n in pool], scorer=fuzz.token_set_ratio, limit=_CANDIDATES * 3)
    ids: list[int] = []
    by_label = {}
    for eid, label in pool:
        by_label.setdefault(label, eid)
    for label, score in hits:
        eid = by_label[label]
        if score >= _MIN_SCORE and eid not in ids:
            ids.append(eid)
        if len(ids) >= _CANDIDATES:
            break
    if ids:
        marks = ",".join("?" * len(ids))
        fam = [r[0] for r in con.execute(f"""
            SELECT parent_id FROM entity_links WHERE child_id IN ({marks})
            UNION SELECT child_id FROM entity_links WHERE parent_id IN ({marks})
            LIMIT {_FAMILY}""", ids + ids).fetchall()]
        ids += [f for f in fam if f not in ids]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return con.execute(f"SELECT entity_id, name FROM entities WHERE entity_id IN ({marks})", ids).fetchall()


def _describe(con: duckdb.DuckDBPyConnection, eid: int) -> str:
    name, typ, ticker = con.execute("SELECT name, type, ticker FROM entities WHERE entity_id = ?", [eid]).fetchone()
    aliases = [r[0] for r in con.execute("SELECT alias FROM entity_aliases WHERE entity_id = ? AND alias <> ? "
                                         "LIMIT 5", [eid, name]).fetchall()]
    links = [f"{rel} {other}" for rel, other in con.execute("""
        SELECT l.relation, e.name FROM entity_links l JOIN entities e ON e.entity_id = l.parent_id
        WHERE l.child_id = ? LIMIT 3""", [eid]).fetchall()]
    bits = [typ] + ([ticker] if ticker else []) + (["aliases: " + "; ".join(aliases)] if aliases else []) + links
    return f"{name} ({', '.join(bits)})"


def decide_entities(con: duckdb.DuckDBPyConnection, cfg: RoboticsConfig, client: Any, *, text: str,
                    entities: list[dict[str, Any]]) -> dict[str, Decision]:
    """One LLM call for every extracted name that has no exact match. Returns
    {extracted resolved-name: Decision}; names absent from the result fall
    back to tier 3 in resolve_entity. Raises on API failure (caller logs)."""
    from .extract import _gen_config  # noqa: PLC0415
    from .retry import call_with_retry  # noqa: PLC0415

    todo: dict[str, list[tuple[int, str]]] = {}
    for e in entities:
        name = (e.get("resolved") or e.get("mention") or "").strip()
        if not name or name in todo or db.find_entity_exact(con, name) is not None \
                or (e.get("mention") and db.find_entity_exact(con, e["mention"]) is not None):
            continue
        cands = _candidates(con, name)
        if cands:   # nothing close at all → plainly new, no call needed
            todo[name] = cands
    if not todo:
        return {}
    names_txt = "\n".join(f"- {n}\n" + "\n".join(f"    · {_describe(con, eid)}" for eid, _ in c)
                          for n, c in todo.items())
    prompt = _PROMPT.format(text=text[:4000], names=names_txt)
    model = cfg.extraction.model
    resp = call_with_retry(
        lambda: client.models.generate_content(
            model=model, contents=prompt,
            config=_gen_config(model, cfg.extraction.direction_thinking_level, temperature=0.0,
                               max_output_tokens=16384, response_mime_type="application/json",
                               response_schema=NameDecisions)),
        op="entity_decide", context={"names": len(todo)})
    parsed: NameDecisions | None = resp.parsed  # type: ignore[assignment]
    if parsed is None:
        raise RuntimeError(f"entity_decide: no parseable output ({(resp.text or '')[:200]!r})")

    bar = cfg.resolution.min_confidence
    out: dict[str, Decision] = {}
    for d in parsed.names:
        cands = {n: eid for eid, n in todo.get(d.name, [])}
        if not cands:
            continue
        dec = Decision(confidence=d.confidence, reason=d.reason)
        for kind, other in (("same_as", d.same_as), ("subsidiary_of", d.parent), ("sister_of", d.sister_of)):
            if other not in cands:
                continue
            if d.confidence < bar:
                dec.review.append(f"{kind} {other}")
            elif kind == "same_as":
                dec.same_as = cands[other]
            else:
                dec.links.append((cands[other], kind))
        if dec.same_as is not None:
            dec.links = []          # a same-as name gets no structure of its own
        out[d.name] = dec
    return out


def _lookup(con: duckdb.DuckDBPyConnection, resolved_name: str, mention: str,
            decision: Decision | None) -> tuple[int, int, str] | None:
    """Tiers 1-2 without writing: (entity_id, tier, matched_name), or None if
    the name would become a new entity."""
    hit = db.find_entity_exact(con, resolved_name)
    if hit is not None:
        return hit, 1, resolved_name
    # The raw mention may match an existing name/alias even when the LLM's
    # `resolved` form differs.
    if mention and mention != resolved_name:
        hit = db.find_entity_exact(con, mention)
        if hit is not None:
            return hit, 1, mention
    if decision is not None and decision.same_as is not None:
        name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [decision.same_as]).fetchone()
        return decision.same_as, 2, name
    return None


def resolve_entity(
    con: duckdb.DuckDBPyConnection,
    *,
    resolved_name: str,
    mention: str,
    ticker: str | None,
    type_: str,
    catalyst_id: int,
    cfg: RoboticsConfig,
    decision: Decision | None = None,
) -> ResolveResult:
    """Return an entity_id for this mention, creating/aliasing as needed."""
    resolved_name, mention = resolved_name.strip(), (mention or "").strip()
    found = _lookup(con, resolved_name, mention, decision)
    if found is not None:
        eid, tier, matched = found
        if tier == 1 and matched == resolved_name and mention and mention.lower() != resolved_name.lower():
            db.insert_alias(con, eid, mention)        # exact on resolved: remember the raw mention
        elif tier == 2:                                # LLM same-as: both spellings resolve next time
            db.insert_alias(con, eid, resolved_name)
            if mention and mention != resolved_name:
                db.insert_alias(con, eid, mention)
        return ResolveResult(entity_id=eid, tier=tier, matched_name=matched)

    # Tier 3 — new entity. Create it AND log to unresolved_entities for review;
    # record any corporate links the LLM was sure of.
    new_id = db.insert_entity(con, name=resolved_name, ticker=ticker, type_=type_)
    if mention and mention != resolved_name:
        db.insert_alias(con, new_id, mention)
    db.insert_unresolved(
        con,
        mention=mention or resolved_name,
        catalyst_id=catalyst_id,
        suggested_name=resolved_name,
        suggested_type=type_,
    )
    if decision is not None:
        from .entities import link  # noqa: PLC0415

        for other, rel in decision.links:
            link(con, new_id, other, rel, confidence=decision.confidence, reason=decision.reason, source="ingest")
    return ResolveResult(entity_id=new_id, tier=3, matched_name=resolved_name)

