# Ticket — Admin: edit an edge's evidence type, mechanism and subtype

Status: built 2026-11-16 · admin only

## Why (ROB-092126-004 and many like it)
That card has no source article (`source_url` empty): the extractor saw only the one-sentence JPMorgan
finding plus its own Pass-1 web research. "Mercedes-Benz / GXO / Jabil pilots Apptronik" and
"Hyundai / DeepMind deploys Atlas" came from that research, yet were labelled `direct` with no
`source_refs`. It was extracted 2026-09-21, before the 2026-11-10 evidence bar. Prod count (2026-11-16):
of 3,689 live `direct` edges, 689 sit on cards with no article where at least one end is not named in the
card's own text — they can only have come from research (should be `web_grounded` if cited, else
`inferred`). Cards with an article can't be judged from stored text (the article isn't stored).

## Requirements
- R1 Edit form (relationship pane → Edit) gains **Evidence** (direct / inferred / web_grounded /
  speculative), **Subtype** (auto = rules, or one of the type's subtypes) and **Mechanism** (editable).
  Add-edge gains Evidence and Subtype too.
- R2 New op `edit_edge {rel_id, evidence_type?, mechanism?, rel_subtype?, expect}` — only the given fields,
  each guarded by `expect` (what the admin saw). One audit row, revertible (inverse edit_edge).
- R3 Subtype: choosing one stores it with `rel_subtype_locked = true`; `subtypes.refresh()` never
  overwrites a locked subtype while it belongs to the row's type. A retype lifts the lock (the rules decide
  again). Choosing "auto" unlocks and the rules recompute at once. The pane shows "(set by admin)".
- R4 Guards: evidence in the 4 types; subtype must belong to the edge's type; mechanism ≥ 10 chars.
- R5 Same downstream as every edit: hashtags + subtypes refresh, card/entity docs and embeddings
  refreshed by the save (a mechanism change re-embeds the card).
- R6 Additive column `relationships.rel_subtype_locked BOOLEAN` (db.init_schema + admin ensure_schema);
  hard-delete backups copy every column, so the lock survives delete/revert.

## Review follow-up (CI review, 2026-11-16)
- Reverting an edit_edge restores EXACTLY what was there (a NULL or short original mechanism included),
  guarded by the recorded after-state — not through the forward op's input guards.
- A retype (repoint_edge) remembers a hand-set subtype it lifts (`_locked_subtype` in its audit row);
  reverting the retype puts that locked subtype back.
- UI: mechanism length is checked before any call (no half-applied save); add-edge confirm names the chosen
  evidence; subtype tooltips say whether it was set by admin.
- DEPLOY: ship the ingest service together with the admin service. The daily full `subtypes.refresh()` in
  ingest must be the lock-aware version, or it would overwrite hand-set subtypes.

## Not in this ticket (proposed next)
A one-time bulk correction of the 689 no-article `direct` edges (→ `web_grounded` when cited, else
`inferred`), as audited, revertible admin edits — only with the user's go.

## Tests
tests/test_admin_edit_edge.py — edit + revert; lock survives refresh and a mechanism change; auto restores
the rules value; retype lifts the lock; guards; add_edge with evidence + locked subtype.
