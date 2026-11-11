# Ticket — Admin: Rejected list (bulk Restore / Delete permanently) + stronger Delete-entity guard

Status: built 2026-11-11 · Items 2 and 4 of the 2026-11-11 delivery list · admin only

## Item 2 — Rejected list
- R1 A "Rejected (N)" button in the admin header opens one list of every `rejected` relationship across all
  entities (newest card first): checkbox, `A —type→ B`, card id + date + headline, mechanism.
  Self-loops are listed too, labelled "internal event" (e.g. Tesla's 10 merge-rejected rows).
- R2 Bulk **Restore**: selected rows → `active`, or → `internal` for a self-loop
  (docs/ticket-internal-events.md). Each row goes through `apply_edit("set_edge_status")` with
  `expect: {status: rejected}` — same guards (live-twin, self-loop), one audit row each, each revertible.
- R3 Bulk **Delete permanently**: selected rows → `apply_edit("delete_edge")` with `expect: {status: rejected}`
  (hard delete, row saved in the audit for revert). Confirm dialog states the count.
- R4 All-or-nothing per save (like repoint_many): any refusal (row changed meanwhile, live twin) aborts the
  save, nothing is pushed, the message names the row. One push + one card/vector refresh per save.
- R1a (2026-11-11) Filter by entity (substring, either end) and "select all shown": select-all acts on
  the rows on screen only, and narrowing the filter drops hidden rows from the selection, so Restore /
  Delete never touch a row you can't see. The list says what Restore means: the row comes back exactly as
  it is now (no merge / repoint is undone); a self-loop comes back as an internal event.
- R5 Review inbox: rel_ids of the batch are passed to auto-resolve like other edge ops.
- R5a (CI audit, 2026-11-11) Restore / repoint never create a live twin, and for a two-party type the
  REVERSED pair is a twin too (`_live_twin`) — else dedupe would silently delete one of them. The header
  button shows "Rejected (N)"; the list shows the newest 2000 and says so when there are more. Two-party
  self-loops are tagged "delete only" and can't be restored.

## Item 4 — Delete entity with ACTIVE edges
- R6 If the entity has any live (active/materialized) relationship, a click-confirm is not enough: the
  admin must type the entity's exact name. Entities with only rejected/invalidated edges, or none, keep
  the simple confirm.
- R7 The server enforces it too: `delete_entity` with live edges requires `confirm_name` == the entity's
  current name (exact), else EditError — a stale page or a scripted call can't skip it.
