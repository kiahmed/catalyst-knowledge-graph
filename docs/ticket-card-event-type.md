# Ticket — Card event type (`catalysts.event_type`)

Status: built 2026-11-11 · Item 6 of the 2026-11-11 delivery list

## Why
Edges say how two companies relate; nothing says what HAPPENED in the story (a launch, a round, a factory).
That gap forced events into edges (self-loops, "competes_with" as a catch-all — data/relationship_type_gap_20261111.txt).
A card-level label answers it without an LLM decision and without touching edges.

## Requirements
- R1 Additive, nullable `catalysts.event_type`; no existing column, edge, count or prompt changes.
- R2 One of: ipo_listing · m_and_a · funding · product_launch · production_facility · deployment ·
  partnership · regulation · research · leadership; NULL when the headline reports none of these
  (analysis, forecasts, records, commentary). Never forced.
  (deployment and leadership were added to the 8 proposed in the type-gap report: they were the largest
  misses — contracts/orders/deployments and CEO changes.)
- R3 Deterministic from the HEADLINE only (`src/events.py`). The earliest cue wins (the main verb comes
  first); an IPO cue wins from anywhere; "launches a factory / a fund / a national initiative" goes by the
  object. The story text is NOT used (tested: it mostly added wrong labels).
- R4 Computed at ingest for each new card, and fully at every ingest run (`events.refresh(con)`, writes
  only changes: first run after deploy = backfill, a rules change self-heals). Headlines are never edited
  by admin, so no admin hook.
- R5 Exported as `event_type` on every card doc / cards.json (null when none or DB not yet migrated).
  Not in embeddings, not on PNGs, not in the UI yet.

## Validation (data/event_type_eval_20261111.txt)
All 889 prod cards. Three tuning rounds of hand-reading 10 per type; final: funding/ipo/m_and_a 10/10,
partnership ~10/10, leadership 4/4, deployment/launch ~8/10, facility/regulation ~7/10, research ~6/8.
Blind Gemini check 31/34 (two diffs fixed after; one is a two-event headline where rules take the first).

## Tests
tests/test_events.py — prod headlines per type, every false positive found while tuning, refresh idempotence.
