"""3-tier entity resolver.

Tier 1 — exact match on entities.name or entity_aliases.alias (case-insensitive).
Tier 2 — fuzzy match (thefuzz.ratio) against all known names; if above
         threshold, reuse that entity and record the mention as an alias.
Tier 3 — unresolved: insert a new entity (type=the LLM's suggested type), and
         log the mention in unresolved_entities for later human review.

All three tiers return an entity_id, so relationships can always be written.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb
from thefuzz import process as fuzz_process

from . import db
from .config import RoboticsConfig


@dataclass
class ResolveResult:
    entity_id: int
    tier: int           # 1 = exact, 2 = fuzzy, 3 = new/unresolved
    matched_name: str   # canonical name the mention resolved to


def resolve_entity(
    con: duckdb.DuckDBPyConnection,
    *,
    resolved_name: str,
    mention: str,
    ticker: str | None,
    type_: str,
    catalyst_id: int,
    cfg: RoboticsConfig,
) -> ResolveResult:
    """Return an entity_id for this mention, creating/aliasing as needed."""
    # Tier 1 — exact
    hit = db.find_entity_exact(con, resolved_name)
    if hit is not None:
        # Record the raw mention as an alias if it differs from canonical.
        if mention and mention.lower() != resolved_name.lower():
            db.insert_alias(con, hit, mention)
        return ResolveResult(entity_id=hit, tier=1, matched_name=resolved_name)

    # Also try the mention itself in case LLM's `resolved` form differs but
    # the raw mention matches an existing alias.
    if mention and mention != resolved_name:
        hit = db.find_entity_exact(con, mention)
        if hit is not None:
            return ResolveResult(entity_id=hit, tier=1, matched_name=mention)

    # Tier 2 — fuzzy match against all known canonical names
    all_names = db.all_entity_names(con)
    if all_names:
        names_only = [n for _, n in all_names]
        best = fuzz_process.extractOne(resolved_name, names_only)
        if best and best[1] >= cfg.resolution.fuzzy_threshold:
            matched_name = best[0]
            matched_id = next(eid for eid, n in all_names if n == matched_name)
            db.insert_alias(con, matched_id, resolved_name)
            if mention and mention != resolved_name:
                db.insert_alias(con, matched_id, mention)
            return ResolveResult(entity_id=matched_id, tier=2, matched_name=matched_name)

    # Tier 3 — new entity. Create it AND log to unresolved_entities for review.
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
    return ResolveResult(entity_id=new_id, tier=3, matched_name=resolved_name)
