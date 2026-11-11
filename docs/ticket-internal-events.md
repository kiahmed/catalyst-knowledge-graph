# Ticket — Internal events (self-loop edges kept, shown only as a node-panel row)

Status: built 2026-11-11 · Item 1 of the 2026-11-11 delivery list

## 1. Why
A self-loop `Tesla --deploys--> Tesla` is real news when it comes from "Tesla deploys Optimus in its own
factory": the product (Optimus, AI5) was folded into its company by a merge or an alias, and the event now
sits on one entity. Today merge **rejects** such rows and ingest **skips** them, so the event is lost from the
company's history. We keep them — without drawing them as edges or changing any between-entity count.

## 2. Design
An internal event is a self-loop row with the new status **`internal`**. Every live view, count and
consumer already selects `status IN ('active','materialized')`, so internal rows are excluded everywhere
by construction; the `status <> 'rejected'` COUNT queries (export stats, social picker) now also exclude
`internal`. Other `<> 'rejected'` readers deliberately still see internal rows, since they are real events
about the entity: card chips (export card entities, render chips), LLM/handle context (entities.py
evidence + audit context, handle_sweep context, dev-utils/entity_audit). None counts or draws edges.
Only symmetric (two-party) types are never internal: "X partners_with X" is noise.

## 3. Requirements
- R1 Between-entity edges, counts and views are unchanged: card relationships list, graph edges, graph
  "seen in N", node-panel top 5 / edge_count / heat, recap, hashtags, embeddings, entity docs' card_count,
  card PNGs — all keep excluding `entity_a_id = entity_b_id` (they already do).
- R2 Merge: a LIVE self-loop it creates becomes `internal` (directional type) or `rejected` (symmetric);
  non-live rows keep their status (never revived).
- R3 Ingest DROPS every self-loop (as before this ticket). It can't tell an internal event ("Optimus" →
  Tesla) from a bad alias ("Samsung SDS" → Samsung SDI makes "SDS supplies SDI" a fake SDI event) — CI
  audit W1, 2026-11-11. Internal events come only from a deliberate admin merge (R2).
- R4 Each card doc gets an additive `internal_events: [{entity, rel, rel_subtype, mechanism, confidence,
  evidence_type}]` (its `internal` rows, min_edge_confidence applied, one per (entity, rel), [] when none).
- R5 Node panel: when an entity has internal events, one row "Internal events (N)" (N = cards) under
  "Top edges"; clicking it opens the Cards tab filtered to exactly those cards (existing `showCardsFor`).
  The expanded card shows its normal between-entity edges; the self-loop itself is not drawn.
- R6 Admin: `internal` is a valid edge status only for a self-loop; a self-loop can never be set live
  (active/materialized); no duplicate internal event per card (also on a revert's re-insert). The entity
  pane lists its internal events with Reject / Delete (CI audit W2: a wrong one — e.g. from a merge of two
  duplicate entities — must be fixable in the UI); the Relationships column still hides self-loops.

## 3a. Review follow-up (4ae9b6e review)
- An `internal` row can't be repointed (it would leave a non-loop `internal` row no view shows).
- A symmetric type can't be set `internal` by admin (matches §2).
- Merge keeps ONE internal event per (card, type): two products folded into one company on one card
  ("deploys Optimus", "deploys AI5") keep the most confident row; restatements are deleted, like dedupe_rows.
- Export: `internal_events: []` is on every card, so the first export after deploy rewrites every card doc
  once (together with `rel_subtype` and `event_type`); a card whose only row for an entity is internal
  still lists that entity as a chip (it is on the card).

## 4. Data step after deploy
Set Tesla's 10 self-loops that the 2026-11-10 Optimus/AI5 merges set `rejected` (all directional:
deploys ×5, pilots, integrates_with, built_on, supplies, spins_out_from) to `internal` via the admin
`set_edge_status` path (audited, revertible), then refresh those cards.

## 5. Consumers
Firestore card docs are safe: `internal` never appears in `relationships[]`; card docs only gain
`internal_events`. UNVERIFIED (CI audit W5, tracked in implementation_spec): if Traction Engine reads the
DuckDB `relationships` table directly with a `status <> 'rejected'` filter (not `IN ('active',
'materialized')`), internal self-loops would reach it as edges — confirm its filter before the Tesla
data step.

## 6. Tests
tests/test_internal_events.py — merge (directional → internal, symmetric → rejected, dead rows not
revived), ingest rule, card export (relationships + stats identical, internal_events filled once, graph has
no self-loop edge), admin status guard + revert.
