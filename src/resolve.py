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

from . import db, names
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
  share an owner that is not listed give sister_of. State-owned enterprises,
  government agencies and subsidiaries are NEW with the owner as parent
  (China Telecom → parent China), never same_as their owner.

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


# ── Classification (docs/ticket-ingest-entity-resolution.md §3.3) ──

def _alias_trusted(alias: str, ent_name: str, ticker: str | None, verified: bool | None) -> bool:
    """A legacy (unverified) alias is still safe when it is just the name
    written differently, the ticker, or the name's initials."""
    if verified:
        return True
    ak = names.strict_key(alias)
    if ak == names.strict_key(ent_name):
        return True
    if ticker and alias.strip().lstrip("$").upper() == ticker.upper():
        return True
    initials = "".join(w[0] for w in names.strict_key(ent_name).split())
    return len(initials) >= 2 and ak.replace(" ", "") == initials


_KEY_INDEX: dict[str, Any] = {"stamp": None}


def _key_index(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """strict_key → rows for entity names and aliases. Rebuilt whenever the
    entity/alias tables change (hash over every name / alias / trust flag)."""
    stamp = con.execute("""SELECT (SELECT hash(string_agg(CAST(entity_id AS VARCHAR) || '|' || name || '|' || COALESCE(ticker, ''),
                                                  '~' ORDER BY entity_id)) FROM entities),
                                  (SELECT hash(string_agg(CAST(entity_id AS VARCHAR) || '|' || alias || '|' ||
                                                  COALESCE(CAST(verified AS VARCHAR), ''), '~' ORDER BY alias_id))
                                     FROM entity_aliases)""").fetchone()
    if _KEY_INDEX["stamp"] != stamp:
        by_name: dict[str, list] = {}
        for i, n in con.execute("SELECT entity_id, name FROM entities").fetchall():
            by_name.setdefault(names.strict_key(n), []).append((i, n))
        by_alias: dict[str, list] = {}
        for r in con.execute("""SELECT a.entity_id, a.alias, a.verified, e.name, e.ticker
                                FROM entity_aliases a JOIN entities e USING (entity_id)""").fetchall():
            by_alias.setdefault(names.strict_key(r[1]), []).append(r)
        _KEY_INDEX.update(stamp=stamp, by_name=by_name, by_alias=by_alias)
    return _KEY_INDEX


def _match(con: duckdb.DuckDBPyConnection, name: str) -> tuple[int, str] | None:
    """(entity_id, how) for a name: how = 'name' (canonical, trusted), 'alias'
    (trusted alias) or 'unverified' (legacy alias that needs an LLM check)."""
    if not name:
        return None
    key = names.strict_key(name)
    rows = con.execute("SELECT entity_id, name FROM entities WHERE lower(name) = lower(?)", [name]).fetchall()
    idx = None
    if not rows:
        idx = _key_index(con)
        rows = idx["by_name"].get(key, [])
    if rows:
        return rows[0][0], "name"
    cands = con.execute("""SELECT a.entity_id, a.alias, a.verified, e.name, e.ticker FROM entity_aliases a
                           JOIN entities e USING (entity_id) WHERE lower(a.alias) = lower(?)""", [name]).fetchall()
    if not cands:
        cands = (idx or _key_index(con))["by_alias"].get(key, [])
    if not cands:
        return None
    for eid, alias, verified, ent_name, ticker in cands:          # prefer a trusted one
        if _alias_trusted(alias, ent_name, ticker, verified):
            return eid, "alias"
    return cands[0][0], "unverified"


def classify(con: duckdb.DuckDBPyConnection, resolved_name: str, mention: str) -> tuple[str, int | None]:
    """How ingest should place one extracted name, before any write:
    ('trusted', id)  canonical name / trusted alias — no LLM;
    ('generic', None) a descriptor ("Chinese telecom company") — group entity;
    ('check', id)    hit only via a legacy alias, or the extractor rewrote the
                     mention to this entity — the LLM must confirm it;
    ('new', None)    no hit — the existing LLM tier decides."""
    resolved_name, mention = (resolved_name or "").strip(), (mention or resolved_name or "").strip()
    rewritten = mention and names.strict_key(mention) != names.strict_key(resolved_name)
    m = _match(con, mention) if rewritten else None
    if m and m[1] in ("name", "alias"):
        return "trusted", m[0]                     # the text itself names a known entity
    if names.is_generic(mention):
        return "generic", None                     # never an alias of a real company
    r = _match(con, resolved_name)
    if r and r[1] in ("name", "alias") and not rewritten:
        return "trusted", r[0]
    if r:          # via a legacy alias, or the extractor's rewrite of the mention: not evidence alone
        return "check", r[0]
    if m:
        return "check", m[0]
    return "new", None


def group_entity(con: duckdb.DuckDBPyConnection, display: str, mention: str) -> tuple[int, bool]:
    """One entity per generic phrase (names.loose_key): "leading logistics
    companies in China" and "Chinese leading logistic company" land on the same
    one. Reuses a type='group' entity, or a generic-named legacy entity, with
    the same key; else creates type 'group'. Returns (entity_id, created)."""
    key = names.loose_key(mention or display)
    rows = con.execute("SELECT entity_id, name, type FROM entities").fetchall()
    for want_group in (True, False):   # an explicit group wins over a generic-named legacy entity
        for eid, n, typ in rows:
            if (typ == "group") == want_group and (want_group or names.is_generic(n)) and names.loose_key(n) == key:
                return eid, False
    for eid, alias in con.execute("""SELECT a.entity_id, a.alias FROM entity_aliases a JOIN entities e USING (entity_id)
                                     WHERE e.type = 'group'""").fetchall():
        if names.loose_key(alias) == key:
            return eid, False
    # Named after the phrase AS WRITTEN — never the extractor's rewrite target:
    # "Chinese telecom companies" rewritten to "China Telecom" must not create a
    # group called "China Telecom" that would then capture the real company.
    name = (mention or "").strip() if names.is_generic(mention or "") else ""
    if not name:
        name = display.strip() if names.is_generic(display) else (mention or display).strip()
    name = name[:1].upper() + name[1:]
    if con.execute("SELECT 1 FROM entities WHERE lower(name) = lower(?)", [name]).fetchone():
        name = f"{name} (group)"
    eid = db.insert_entity(con, name=name, ticker=None, type_="group")
    return eid, True


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
    checks: dict[str, int] = {}          # name → entity its legacy alias / rewrite pointed at
    for e in entities:
        name = (e.get("resolved") or e.get("mention") or "").strip()
        if not name or name in todo:
            continue
        kind, hit = classify(con, name, e.get("mention") or name)
        if kind in ("trusted", "generic"):
            continue
        cands = _candidates(con, name)
        if kind == "check":   # the alias/rewrite target + its family must be among the candidates
            checks[name] = hit
            fam = [r[0] for r in con.execute("""SELECT parent_id FROM entity_links WHERE child_id = ?
                                                UNION SELECT child_id FROM entity_links WHERE parent_id = ?""",
                                             [hit, hit]).fetchall()]
            have = {eid for eid, _ in cands}
            extra = [hit] + [f for f in fam if f != hit]
            cands = con.execute(f"SELECT entity_id, name FROM entities WHERE entity_id IN ({','.join('?' * len(extra))})",
                                extra).fetchall() + [c for c in cands if c[0] not in set(extra)]
            cands = cands[:_CANDIDATES + _FAMILY]
            del have
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
        hit = checks.get(d.name)
        if hit is not None and d.confidence >= bar and dec.same_as != hit:
            was, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [hit]).fetchone()
            dec.review.append(f"legacy alias / rewrite pointed {d.name!r} at {was!r}; the LLM says it is not — "
                              "check that alias")
        out[d.name] = dec
    return out


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
    kind, hit = classify(con, resolved_name, mention or resolved_name)
    if kind == "trusted":
        # No alias write: a format variant already matches by strict_key, and an
        # extractor rewrite is not evidence (that's how "China's leading
        # logistics company" became an alias of China).
        name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [hit]).fetchone()
        return ResolveResult(entity_id=hit, tier=1, matched_name=name)
    if kind == "generic":
        gid, created = group_entity(con, resolved_name, mention or resolved_name)
        if mention:
            db.insert_alias(con, gid, mention, verified=True, source="generic")
        if created:
            db.insert_unresolved(con, mention=mention or resolved_name, catalyst_id=catalyst_id,
                                 suggested_name=resolved_name, suggested_type="group")
        name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [gid]).fetchone()
        return ResolveResult(entity_id=gid, tier=1 if not created else 3, matched_name=name)
    if kind == "check" and (decision is None or (decision.same_as is None and not decision.links
                                                 and decision.confidence < cfg.resolution.min_confidence)):
        # LLM failed / unsure / absent: keep today's behavior (the alias match),
        # never a silent new entity; the review flag (if any) goes to an admin.
        log.info("unverified_alias_fallback name=%r entity_id=%s", resolved_name, hit)
        name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [hit]).fetchone()
        return ResolveResult(entity_id=hit, tier=1, matched_name=name)
    if decision is not None and decision.same_as is not None:
        eid = decision.same_as
        db.insert_alias(con, eid, resolved_name, verified=True, source="ingest_llm")   # confirmed on this card
        if mention and mention != resolved_name:
            db.insert_alias(con, eid, mention, verified=True, source="ingest_llm")
        name, = con.execute("SELECT name FROM entities WHERE entity_id = ?", [eid]).fetchone()
        return ResolveResult(entity_id=eid, tier=2, matched_name=name)

    # Tier 3 — new entity. Create it AND log to unresolved_entities for review;
    # record any corporate links the LLM was sure of.
    if kind == "check":   # the LLM said this is NOT the legacy alias's entity
        log.info("unverified_alias_contradicted name=%r was_entity_id=%s", resolved_name, hit)
    if con.execute("SELECT 1 FROM entities WHERE lower(name) = lower(?)", [resolved_name]).fetchone():
        resolved_name = mention or resolved_name   # canonical form taken by the entity it is NOT
    new_id = db.insert_entity(con, name=resolved_name, ticker=ticker, type_=type_)
    if mention and mention != resolved_name:
        db.insert_alias(con, new_id, mention, verified=True, source="ingest_new")
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

