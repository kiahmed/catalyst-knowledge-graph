# Ticket — Ingest: a `direct` edge needs both ends named in the card's own text

Status: built 2026-11-19 · ingest only · deterministic, no LLM

## Why
The extractor labels research-derived claims `direct`. Live test 2026-11-19: a story naming only Tesla/Optimus
produced "Agility Robotics competes_with Tesla" as `direct`; a Jetson Thor story produced "Jabil supplies
Apptronik" and "Agility Robotics supplies GXO" as `direct`. The 2026-11-17 bulk fix corrected 671 stored edges,
but nothing stopped new ones — the ingest code had no such rule.

## Rule (src/ingest.py `story_text`, `checked_evidence`, applied in `write_extraction` before each insert)
- R1 The card's OWN text = headline + finding + the fetched source article (enrichment path `url_fetch`, ok).
  Never the extractor's grounding research: when a card has no article, `context_text` IS that research and
  must not make a claim look direct.
- R2 A `direct` edge stays `direct` only if BOTH ends are named in that text — by canonical name or alias
  (`story.mentioned`), or by the name the extractor used (`names.mentioned_in`). Each end is paired with its
  own extracted name BEFORE the symmetric (two-party) reordering.
- R3 Otherwise: `web_grounded` if the edge cites a source (`source_refs`), else `inferred`. Logged as
  `evidence_downgraded` with which end wasn't named; recorded on the extraction (`evidence_downgraded`).
- R4 Edges touching a `group` bucket are left alone (a generic bucket can't be matched by name). Non-direct
  edges are untouched.
- R5 No relationship type requires `direct` (the highest `min_evidence` is `inferred`), so a downgraded edge
  still meets its type's bar — nothing is dropped. A `competes_with` edge's subtype follows (`rivals` →
  `pressures` unless the mechanism says "directly compete…"), via the normal subtype refresh.

## Tests
tests/test_ingest_direct_check.py — story text excludes research and keeps a real article; both named → direct;
one missing → inferred / web_grounded (cited); alias counts; group and non-direct untouched; end-to-end
write_extraction (a two-party edge stored in swapped order — the case that exposed the pairing bug).
