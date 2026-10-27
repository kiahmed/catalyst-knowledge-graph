"""Is an entity part of THIS card's story? Deterministic, no LLM.

An entity is "in the story" when the card's own news text (headline + raw
finding) names it — by canonical name, a known alias, or the name without a
"(TICKER)" suffix — as a whole word. An edge is in the story when either end
is. Everything else on a card is BACKGROUND: real relationships the extractor
found while researching the catalyst (web grounding), valid for the full
graph, shown dimmed on the card's mini-graph/poster.

Relevance only: this says nothing about whether an edge is true — that is
`evidence_type` (direct / web_grounded / inferred / speculative).
"""
from __future__ import annotations

import re
from collections import defaultdict

import duckdb

_TICKER = re.compile(r"\s*\(.*?\)")
_MIN_KEY_LEN = 2   # "3M"; keys shorter than _CASE_LEN must match exact case
_CASE_LEN = 3
# Entity names that are also everyday words: match only in their proper-noun
# casing ("Figure raised…", not "a key figure"). Prod check 2026-10-27 found
# Atlas, Optimus, Bolt, Figure, Apple; the rest are pre-emptive.
COMMON_WORDS = {"atlas", "optimus", "bolt", "figure", "apple", "arm", "spot",
                "digit", "scale", "unity", "shield", "apex", "nova", "edge", "core"}


def load_aliases(con: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    """canonical name → match keys in ORIGINAL case (name, aliases, ticker-stripped)."""
    keys: dict[str, set[str]] = defaultdict(set)
    for name, alias in con.execute(
        "SELECT e.name, a.alias FROM entities e LEFT JOIN entity_aliases a USING (entity_id)"
    ).fetchall():
        for k in (name, alias):
            if k:
                keys[name].add(k.strip())
                keys[name].add(_TICKER.sub("", k).strip())
    return keys


def _hit(key: str, text: str) -> bool:
    if len(key) < _MIN_KEY_LEN:
        return False
    exact = len(key) < _CASE_LEN or key.lower() in COMMON_WORDS
    pat = r"(?<![A-Za-z0-9])" + re.escape(key) + r"(?![A-Za-z0-9])"
    return re.search(pat, text or "", 0 if exact else re.IGNORECASE) is not None


def mentioned(name: str, text: str, aliases: dict[str, set[str]]) -> bool:
    cands = aliases.get(name) or {name, _TICKER.sub("", name).strip()}
    return any(_hit(k, text) for k in cands)
