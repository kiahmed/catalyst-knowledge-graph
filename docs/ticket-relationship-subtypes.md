# Ticket — Relationship subtypes (`rel_subtype`)

Status: built 2026-11-11 · Owner: CKG Robotics · Consumers: Traction Engine, site, admin

## 1. Why
The 15 parent `rel_type`s are too coarse for Traction Engine replies ("supplies" can be contract manufacturing,
IP licensing or a reseller deal; "competes_with" can be a named rival or one line of inferred pressure). We want
that finer meaning **without** asking the LLM to decide more and **without** changing any existing count.

## 2. Rules of the change (hard requirements)
- R1 `rel_type` is unchanged everywhere — values, vocabulary, extraction prompt, response schema, counts.
- R2 `rel_subtype` is an **additive, nullable** column on `relationships`. NULL = plain parent.
- R3 A subtype always belongs to exactly one parent (`subtypes.SUBTYPES`); it rolls up, so recap, graph
  "seen in N", node-panel top 5, card PNGs and Traction Engine filters on `rel_type` are unaffected.
- R4 Deterministic — `src/subtypes.classify()` reads only what the row already has: `rel_type`, `mechanism`,
  `evidence_type`, endpoint names, source entity type. No LLM, no network.
- R5 Precision over coverage: no explicit textual signal → NULL. Direction-sensitive subtypes need the
  source to be the actor (named before the manufacturing verb/role) or they stay NULL.
- R6 Always consistent with the row: recomputed whenever a card's edges change, and fully at every ingest run.

## 3. Vocabulary (parent → subtypes)
| parent | subtype | means (on `A --parent--> B`) |
|---|---|---|
| supplies | manufactures_for | A makes product/parts specifically for B (contract/production partner, "manufactures … for B", "fabrication of B's …") |
| supplies | licenses_to | A licenses IP/technology to B |
| supplies | sells_through | B is a distributor/reseller/sales channel for A's products |
| invests_in | funds | public money: A is a government body, or a grant / subsidy / public funding |
| invests_in | owns | B is A's subsidiary / owned by A |
| partners_with | joint_venture | they formed / share a JV |
| partners_with | research_collaboration | joint research, research center/lab, academic research partnership |
| partners_with | co_development | they jointly develop a product/technology |
| competes_with | rivals | competition stated by the story (`evidence_type = direct`) or "directly compete / direct rival / key competitor" |
| competes_with | pressures | otherwise — inferred/speculative competitive pressure (every competes edge with a mechanism gets one of the two) |

Other 11 parents: no subtypes.

## 4. Where it is computed
- `subtypes.refresh(con, catalyst_ids=None)` — recompute, write only changed rows (one set-based UPDATE).
- `hashtags.store_for_catalyst()` calls `refresh(con, [cid])` — the existing per-card chokepoint every
  edge-changing path already calls (ingest write, every admin edit and revert, entity merge).
- `ingest.run` calls `refresh(con)` after `hashtags.backfill` — the first prod run after deploy **is the
  backfill** (~3.6k rows, ~12 s once); later runs are a 0.3 s no-op unless rules change (self-heal).
- Migration: `ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_subtype VARCHAR` in `db.init_schema`
  and `admin_ops.ensure_schema` (the admin can write before an ingest has migrated). Verified on a prod copy.

## 5. Where it is exported
- Card docs (`catalysts/items/*.relationships[]`) and `cards.json`: new key `rel_subtype` (null when none).
- Graph payload edges: new key `rel_subtype`.
- `export._load_cards` tolerates a DB not yet migrated (exports null).
- Not in embeddings (embed text unchanged → no re-embed), not on card PNGs (no re-render), not in the UI yet.
- Effect of the first prod run: every card doc with at least one relationship gets one Firestore rewrite
  (the new `rel_subtype` key, null included, changes each card's hash) — one-time, then delta as usual.

## 6. Validation (data/subtype_rules_eval_20261111.txt)
All 8,567 live edges classified. Hand-read ~140 random samples over four tuning rounds; final precision
14/14 manufactures_for, 10/10 licenses_to, 10/11 sells_through, 14/14 funds, 14/14 owns, 14/14 joint_venture,
10/10 research_collaboration, ~13/14 co_development, 12/12 rivals, 12/12 pressures. Blind Gemini
cross-check (picks a subtype itself, never sees the rules' answer): 22/24 agree; both diffs are speculative
edges, where certainty is already carried by `evidence_type`.

## 7. Tests
`tests/test_subtypes.py` — rules (incl. every false positive found while tuning: "subsidiary", corporate
"Innovation Fund", "government-backed company", "manufacturers"/catalogue supplier, target-is-maker,
"distributed computing", "distributing … across its group", logistics, "parent company of <other>"),
roll-up, refresh idempotence, rel_type/status untouched, bulk path, per-card hook, admin repoint + revert.

## 7a. Review follow-up (d08d856 review)
Role attribution ("A supplies B, its contract manufacturer" → none), warehouse "distribution centers",
the verb "grants", "owned by / subsidiary of" must name the source, case-insensitive "joint venture",
blank names, generic first words ("General", "Advanced"…) never stand for a company; edit_entity (+ revert)
refreshes subtypes on all of the entity's rows, not only live ones.

## 8. Not in scope
UI display/filter of subtypes; subtypes for the other 11 parents; any LLM involvement.
