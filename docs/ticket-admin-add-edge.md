# Ticket — Admin: Add edge on a card

Status: built 2026-11-11 · Item 3 of the 2026-11-11 delivery list · admin only

## Requirements
- R1 From any relationship's view pane: "Add edge on this card" opens a form for THAT card: Source
  (prefilled with the open entity), Type (vocabulary), Target, Mechanism (required, one sentence). The
  card's entity chips fill Source / Target (src / tgt), like the edit form.
- R2 New op `add_edge {entry_id, entity_a_id, rel_type, entity_b_id, mechanism}`: stored as
  `evidence_type direct`, confidence 1.0, status active, first_flagged_at = card date. The admin is the
  source; the audit row (who, when, ip) records it.
- R3 Guards: card exists; both entities exist; source ≠ target (an edge needs two; internal events come
  from merges/ingest); rel_type in the vocabulary; mechanism ≥ 10 characters; not already live on the
  card (two-party types checked both ways, and stored lower-id first like ingest).
- R4 Same downstream as every edit: hashtags + rel_subtype refresh, card/entity docs + embeddings
  refreshed by the save, review auto-resolve.
- R5 Revert = hard delete of the added row (`delete_edge`, itself revertible), refused if the row's
  status changed since.

## Tests
tests/test_admin_add_edge.py — add + subtype computed + revert; every guard; two-party ordering and the
reversed duplicate.
