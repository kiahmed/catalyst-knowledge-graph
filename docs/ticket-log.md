# Tickets — one running log

All tickets in one file (merged 2026-11-23 from the 12 `docs/ticket-*.md` files). Add new tickets as a
new `## ` section with an `<a id="ticket-…"></a>` anchor; link them as `docs/ticket-log.md#ticket-…`.

## Contents

- [Admin: Add edge on a card](#ticket-admin-add-edge)
- [Admin: edit an edge's evidence type, mechanism and subtype](#ticket-admin-edit-edge-details)
- [Admin: Rejected list (bulk Restore / Delete permanently) + stronger Delete-entity guard](#ticket-admin-rejected-list-delete-guard)
- [Card event type (`catalysts.event_type`)](#ticket-card-event-type)
- [event-level dedup — same story emitted as multiple catalyst cards](#ticket-event-level-dedup)
- [Web app silent refresh (no browser reload needed)](#ticket-frontend-silent-refresh)
- [Graph relationship-type filter (top right of the map)](#ticket-graph-reltype-filter)
- [Ingest: a `direct` edge needs both ends named in the card's own text](#ticket-ingest-direct-evidence-check)
- [ingest-time entity resolution — stop mislabeling at the source](#ticket-ingest-entity-resolution)
- [Internal events (self-loop edges kept, shown only as a node-panel row)](#ticket-internal-events)
- [Relationship subtypes (`rel_subtype`)](#ticket-relationship-subtypes)
- [render fingerprint — know exactly which card images are out of date](#ticket-render-fingerprint)

---

<a id="ticket-admin-add-edge"></a>
## Admin: Add edge on a card

_(was `#ticket-admin-add-edge`)_


Status: built 2026-11-11 · Item 3 of the 2026-11-11 delivery list · admin only

### Requirements
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
  status OR its endpoints / type changed since (CI audit W4: revert the later repoint first).

### Tests
`make test-ui` (admin suite) drives this through the real admin page (test mode, local copy).
tests/test_admin_add_edge.py — add + subtype computed + revert; every guard; two-party ordering and the
reversed duplicate.

---

<a id="ticket-admin-edit-edge-details"></a>
## Admin: edit an edge's evidence type, mechanism and subtype

_(was `#ticket-admin-edit-edge-details`)_


Status: built 2026-11-16 · admin only

### Why (ROB-092126-004 and many like it)
That card has no source article (`source_url` empty): the extractor saw only the one-sentence JPMorgan
finding plus its own Pass-1 web research. "Mercedes-Benz / GXO / Jabil pilots Apptronik" and
"Hyundai / DeepMind deploys Atlas" came from that research, yet were labelled `direct` with no
`source_refs`. It was extracted 2026-09-21, before the 2026-11-10 evidence bar. Prod count (2026-11-16):
of 3,689 live `direct` edges, 689 sit on cards with no article where at least one end is not named in the
card's own text — they can only have come from research (should be `web_grounded` if cited, else
`inferred`). Cards with an article can't be judged from stored text (the article isn't stored).

### Requirements
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

### Review follow-up (CI review, 2026-11-16)
- Reverting an edit_edge restores EXACTLY what was there (a NULL or short original mechanism included),
  guarded by the recorded after-state — not through the forward op's input guards.
- A retype (repoint_edge) remembers a hand-set subtype it lifts (`_locked_subtype` in its audit row);
  reverting the retype puts that locked subtype back.
- UI: mechanism length is checked before any call (no half-applied save); add-edge confirm names the chosen
  evidence; subtype tooltips say whether it was set by admin.
- DEPLOY: ship the ingest service together with the admin service. The daily full `subtypes.refresh()` in
  ingest must be the lock-aware version, or it would overwrite hand-set subtypes.

### Not in this ticket (proposed next)
A one-time bulk correction of the 689 no-article `direct` edges (→ `web_grounded` when cited, else
`inferred`), as audited, revertible admin edits — only with the user's go.

### Tests
`make test-ui` (admin suite) drives this through the real admin page (test mode, local copy).
tests/test_admin_edit_edge.py — edit + revert; lock survives refresh and a mechanism change; auto restores
the rules value; retype lifts the lock; guards; add_edge with evidence + locked subtype.

---

<a id="ticket-admin-rejected-list-delete-guard"></a>
## Admin: Rejected list (bulk Restore / Delete permanently) + stronger Delete-entity guard

_(was `#ticket-admin-rejected-list-delete-guard`)_


Status: built 2026-11-11 · Items 2 and 4 of the 2026-11-11 delivery list · admin only

### Item 2 — Rejected list
- R1 A "Rejected (N)" button in the admin header opens one list of every `rejected` relationship across all
  entities (newest card first): checkbox, `A —type→ B`, card id + date + headline, mechanism.
  Self-loops are listed too, labelled "internal event" (e.g. Tesla's 10 merge-rejected rows).
- R2 Bulk **Restore**: selected rows → `active`, or → `internal` for a self-loop
  (#ticket-internal-events). Each row goes through `apply_edit("set_edge_status")` with
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

### Item 4 — Delete entity with ACTIVE edges
- R6 If the entity has any live (active/materialized) relationship, a click-confirm is not enough: the
  admin must type the entity's exact name. Entities with only rejected/invalidated edges, or none, keep
  the simple confirm.
- R7 The server enforces it too: `delete_entity` with live edges requires `confirm_name` == the entity's
  current name (exact), else EditError — a stale page or a scripted call can't skip it.

### Tests
`make test-ui` (admin suite): reject an edge in the UI → filter the Rejected list by entity → "select all shown" → Restore → active again; Delete entity with live edges refuses a wrong typed name and deletes on the exact one.

---

<a id="ticket-card-event-type"></a>
## Card event type (`catalysts.event_type`)

_(was `#ticket-card-event-type`)_


Status: built 2026-11-11 · Item 6 of the 2026-11-11 delivery list

### Why
Edges say how two companies relate; nothing says what HAPPENED in the story (a launch, a round, a factory).
That gap forced events into edges (self-loops, "competes_with" as a catch-all — data/relationship_type_gap_20261111.txt).
A card-level label answers it without an LLM decision and without touching edges.

### Requirements
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

### Validation (data/event_type_eval_20261111.txt)
All 889 prod cards. Three tuning rounds of hand-reading 10 per type; final: funding/ipo/m_and_a 10/10,
partnership ~10/10, leadership 4/4, deployment/launch ~8/10, facility/regulation ~7/10, research ~6/8.
Blind Gemini check 31/34 (two diffs fixed after; one is a two-event headline where rules take the first).

### CI audit follow-up (2026-11-11)
"Spin-off" counts only as an event ("X prepares a spin-off", "spins off its unit"), not as a company
descriptor ("EPFL Spin-Off Embodied AI Unveils …" → product_launch; the eval had scored it m_and_a).

### Tests
tests/test_events.py — prod headlines per type, every false positive found while tuning, refresh idempotence.

---

<a id="ticket-event-level-dedup"></a>
## event-level dedup — same story emitted as multiple catalyst cards

_(was `#ticket-event-level-dedup`)_


**Filed by:** soljet-postiz (consumer). **Owner:** catalyst-knowledge-graph.
**Date:** 2026-07-19. **Severity:** medium — cosmetic upstream, but publicly visible
and directly billable downstream.

### Summary

The KG dedups **entities** (canonical company records, aliases, `unresolved_entities`).
It does not appear to dedup **events**. When several outlets cover the same funding
round / acquisition / pivot on different dates, each becomes its own catalyst card
with its own `card_id` and `date`. Downstream those are indistinguishable from
genuinely new catalysts.

### Why this repo, not the publisher

soljet-postiz consumes cards and cannot fix this:

- It sees only `headline` / `entities` / `relationships` per card. Matching on
  headline text would be guesswork, and would misfire on the KG's formulaic
  headline templates (see false-positive note below).
- The signals needed to decide "same event" — `source_refs`, extraction
  provenance, canonical entity ids, relationship shape — live here.
- A publisher silently dropping cards would hide a data problem rather than fix it.

### Impact

Measured over the live `CKG-Robotics/catalysts/items` collection (349 cards):

- **10 clusters, 21 cards** (~6%) look like repeat coverage of one event.
- Publishing the backlog untouched posts the same story up to 3× to the same
  audience, weeks apart, with no acknowledgement it's an update.
- On X this is **billable**: ~$0.20 per post, so each duplicate is real money on a
  pay-per-use balance.

### Evidence

Clustered by ≥2 shared entities **and** ≥70% overlap of distinctive headline words
(stopwords like *robotics/humanoid/secures/million/intensifying* removed).
Confidence labels are ours — **the ambiguous ones need a KG judgement call**:

#### Confirmed same event — identical amount, different dates
| card_id | date | headline |
|---|---|---|
| ROB-060426-001 | 2026-06-04 | Generalist AI Secures $400 Million Funding, Intensifying Physical … |
| ROB-071826-004 | 2026-07-18 | Generalist AI Secures $400M to Advance Physical AGI Robotics Found… |

#### Likely same event — same story, re-reported as it developed
| card_id | date | headline |
|---|---|---|
| ROB-041726-001 | 2026-04-17 | Faraday Future Pivots to Embodied AI, Targets 1,000 Robot Shipments |
| ROB-051026-005 | 2026-05-10 | Faraday Future Pivots from EVs to Embodied AI Humanoid Robotics |
| ROB-061426-001 | 2026-06-14 | Faraday Future Pivots into Embodied AI with Humanoid and Quadruped |

| ROB-062426-001 | 2026-06-24 | Agility Robotics Goes Public via $2.5 Billion SPAC Merger |
| ROB-071426-002 | 2026-07-14 | Agility Robotics Goes Public Via SPAC, Raising $620 Million for Hu… |

*(Agility: $2.5B is the valuation, $620M the raise — same SPAC, different figure
emphasised. A reader seeing both would reasonably think two events occurred.)*

#### Uncertain — amounts differ; may be distinct rounds OR revised reporting
| card_id | date | headline |
|---|---|---|
| ROB-031226-007 | 2026-03-12 | Neura Robotics Secures €1 Billion Funding, Intensifying Humanoid R… |
| ROB-071826-002 | 2026-07-18 | NEURA Robotics Secures $1.4 Billion to Accelerate Physical AI Robo… |

| ROB-041026-002 | 2026-04-10 | Zhongqing Robot Achieves Unicorn Status with $200M Series B Led by… |
| ROB-071826-003 | 2026-07-18 | Humanoid Robotics Startup Achieves Unicorn Status with $150M Serie… |

*(Neura: €1B ≈ $1.08B vs $1.4B — a later round, or the same round restated?
Zhongqing: the July headline doesn't name the company and the amount differs, so
it may be a different startup entirely. Only extraction provenance can settle these.)*

### Known false-positive class (please don't "fix" this)

Headline-similarity alone is **not** a usable signal here, because the KG emits
formulaic templates. These are correctly distinct and must stay distinct:

- "AI2 Robotics Secures $735 Million, Intensifying Humanoid Robot Competition"
- "Apptronik Secures $520 Million Mega-Round, Intensifying Humanoid Robot …"
- "Neura Robotics Secures €1 Billion Funding, Intensifying Humanoid Robot …"

Different companies, different rounds, ~86% word overlap. Any dedup must key on
entity + event identity, not text similarity.

### Suggested direction (KG's call)

1. Derive an **event key** at extraction: `(primary_entity_id, event_type,
   normalised_amount, ~time window)` — e.g. `funding_round:neura:1.4B`.
2. On collision, **update the existing catalyst** (refresh `source_refs`, amount,
   date) instead of creating a new card — so later coverage enriches rather than
   duplicates.
3. If a genuinely new development warrants its own card, link it
   (`supersedes` / `follows_from`) so consumers can render it as an update and
   choose to post only the latest.
4. Whatever the mechanism, expose it on the card so consumers can act on it — a
   `superseded_by` field, or simply omitting stale duplicates from the export.

Option 3 is the most useful to us: soljet-postiz already composes a temporal
lead-in ("Back in mid March: …") and could say "update on X" if the link existed.

### What soljet-postiz will do meanwhile

Nothing automatic — we won't guess. The backlog will be posted as-is unless this
lands first, and the operator will eyeball obvious repeats. No publisher-side
dedup is planned, deliberately.

### Repro

```python
## from soljet-postiz repo root
from src.lib.config_loader import load_tier
from bin._common import build_source, load_dotenv
load_dotenv()
t = load_tier("arboryx.robotics")
rows = build_source(t.sources[0], t).list_recent(since=<epoch>, limit=5000)
## cluster on: >=2 shared entity names AND >=70% overlap of headline words
## after removing {robotics, robot, humanoid, secures, launches, million,
## billion, intensifying, competition, targets, production, industrial, scale, funding}
```

---

<a id="ticket-frontend-silent-refresh"></a>
## Web app silent refresh (no browser reload needed)

_(was `#ticket-frontend-silent-refresh`)_


Status: built 2026-11-11 · frontend + export

### Why
robotics.arboryx.ai is a research tool: a page stays open for hours while ingest runs and admin edits
land. Until now new data showed only after a hard reload (e.g. a new internal event on NVIDIA).

### Requirements
- R1 Every lookup — view switch (Cards / Graph / Recap), any card filter or search, any graph filter / time
  window / company search, opening a node panel or a card — calls `maybeSync()`.
- R2 `maybeSync` is cheap and silent: at most one check per 15 s, one in flight, failures only logged.
  A check reads ONE doc (`CKG-<sector>/catalysts`). If its `version` is unchanged, nothing else is read.
- R3 When `version` moved: fetch only card docs with `synced_at >=` (the newest stamp the page holds − 30 min
  overlap, so an export still committing when a later save was synced is picked up), drop the ids in
  `deleted_ids` (entries "<entry_id>@<stamp>") added since,
  merge by `card_id`, rebuild the graph from the cards, re-render the current view; filters, URL hash and
  an open node panel stay as they are. No reload, no spinner.
- R4 Backend (`firestore_export._write_catalysts`): each written card doc gets `synced_at` (one stamp per
  write call; NOT part of the change hash); the parent doc gets `version` = that stamp only when cards were
  written. An admin refresh (`refresh_cards`, partial) no longer overwrites the parent's `count` with the
  few refreshed cards (latent bug fixed).
- R5 Deletes (Arboryx dedup, `upstream_dedup`): append "<entry_id>@<stamp>" (always unique → always appended)
  to the parent's `deleted_ids` and move `version`; best-effort — a failure only means a stale page keeps the
  card until reload. Silent refresh runs only after a signed-in Firestore load (`SYNC.ready`), stops after a
  sign-out in the same tab, keeps the Cards page, re-renders the graph only when it is on screen, and plays no
  tick sound.
- R6 Not refreshed silently: the graph doc's stats / insights / recap (they change once a day with ingest).
  Firestore rules unchanged (signed-in users already read everything under `CKG-*`).

### Review follow-up (CI review, 2026-11-11)
- Silent refresh runs only after the signed-in Firestore load seeded it (`SYNC.ready`) — never on the
  pre-sign-in teaser, where Firebase may not be loaded yet and reads are denied. `checkUpdates` rejects
  (never throws) without the SDK, and the call is wrapped so the busy flag always clears.
- A refresh keeps the user's Cards page (clamped if the list shrank) and re-renders the graph only when
  the Graph tab is on screen.
- Delta window overlaps 30 min before the newest stamp held: stamps are taken before a write commits, so
  an ingest export still writing when an admin save was synced is still picked up.
- Tombstones are "<entry_id>@<stamp>" (always unique → always appended). `deleted_ids` grows by one per
  upstream-dedup delete (a few per month); trim if it ever matters.

### Tests
tests/test_firestore_delta.py `test_silent_refresh_markers`; tests/test_upstream_dedup.py (tombstone format).
`make test-ui` runs the page logic in headless Chromium against a fake Firestore behind the REAL checkUpdates
(its `where('synced_at','>=',…)` is honoured): unseeded page makes no calls; missing SDK rejects and clears
busy; no change → 0 item queries; the overlap window filters old docs; a change merges + applies a tombstone +
keeps the Cards page; throttle; ego view kept through a refresh.


---

<a id="ticket-graph-reltype-filter"></a>
## Graph relationship-type filter (top right of the map)

_(was `#ticket-graph-reltype-filter`)_


Status: built 2026-11-11 · Item 5 of the 2026-11-11 delivery list · frontend only

### Requirements
- R1 A dropdown "Filter by relationship" at the top right of the Graph tab (phone: bottom right). The node
  panel overlays it while open.
- R2 Options: "All relationships", then every parent `rel` present in the loaded graph with its edge count,
  each followed by its subtypes (`rel_subtype`, #ticket-relationship-subtypes) present, indented.
- R3 Parent choice = every edge of that type (subtyped or not). Subtype choice = only edges with that
  subtype. Applied before same-pair aggregation, so "seen in N" counts only matching cards.
- R4 Lives in the URL hash as `rt=<rel>` or `rt=<rel>/<subtype>` (shareable link, like ev/minc/tw);
  "All" removes it. A linked choice absent from the current window stays selectable with (0).
- R5 Same filter everywhere the graph filters edges: graph.js filterPayload (+ render signature), the
  page's window-widening probe (graphEdgeCountFor), node-panel edges (edgesTouching), ego search.
- R6 No other filter or default view changes; with "All" the graph is exactly as before.

### Tests
`make test-ui`: a subtype choice draws only its parent type and exactly filterPayload's edges; "All" removes `rt`;
the dropdown overlaps neither the legend nor the search at 1400/1100/900/700px.

---

<a id="ticket-ingest-direct-evidence-check"></a>
## Ingest: a `direct` edge needs both ends named in the card's own text

_(was `#ticket-ingest-direct-evidence-check`)_


Status: built 2026-11-19 · ingest only · deterministic, no LLM

### Why
The extractor labels research-derived claims `direct`. Live test 2026-11-19: a story naming only Tesla/Optimus
produced "Agility Robotics competes_with Tesla" as `direct`; a Jetson Thor story produced "Jabil supplies
Apptronik" and "Agility Robotics supplies GXO" as `direct`. The 2026-11-17 bulk fix corrected 671 stored edges,
but nothing stopped new ones — the ingest code had no such rule.

### Rule (src/ingest.py `story_text`, `checked_evidence`, applied in `write_extraction` before each insert)
- R1 The card's OWN text = headline + finding + the finding's own takeaways / guidance / price-level text
  (added 2026-11-19: they name companies the one-line finding doesn't) + the fetched source article
  (enrichment path `url_fetch`, ok).
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

### Tests
tests/test_ingest_direct_check.py — story text excludes research and keeps a real article; both named → direct;
one missing → inferred / web_grounded (cited); alias counts; group and non-direct untouched; end-to-end
write_extraction (a two-party edge stored in swapped order — the case that exposed the pairing bug).

---

<a id="ticket-ingest-entity-resolution"></a>
## ingest-time entity resolution — stop mislabeling at the source

_(was `#ticket-ingest-entity-resolution`)_


**Owner:** catalyst-knowledge-graph. **Date:** 2026-11-09. **Status:** built 2026-11-09 (§3.1–3.6), pending review;
migration (§5) not built yet. Defaults taken for §8: (1) generic → tag to a curated `group` bucket or discard (no new entities; see §3.2); (2) generic/place word lists cover the names in today's data; (3) legacy aliases
are unverified unless a safe rule passes (format variant, ticker, initials). Rollout (§6) uses
`make reextract-prod IDS=…` dry runs on known-bad cards instead of 3 log-only days (ingest paused).
**Severity:** high. It writes wrong entity endpoints into prod every day.
**Evidence:** Traction register DA-006/008/009/010/012/013 (2026-11-06..07);
SEER Robotics → Serve Robotics; "Amazon" → AWS; China, Humanoid, "Venture Capital",
"Customers" absorbing descriptor phrases (workbench 2026-11-03, 2026-11-09).

### 1. How ingest resolves names today (`src/resolve.py`, `src/ingest.py`)

Extraction (Pass 1, `src/extract.py`) emits each entity as `mention` (the text as
written) + `resolved` (the LLM's canonical form). For each name:

1. **Tier 1, exact:** `db.find_entity_exact` matches `resolved` or `mention` against
   entity names AND aliases, case-insensitively. A hit is trusted and the LLM never sees it.
   When `resolved` matched and `mention` differs, **the raw mention is saved as a new alias.**
2. **Tier 2, LLM** (`decide_entities`, one call per card): names with no exact
   hit plus fuzzy candidates. The answer is `same_as` (→ reuse, both spellings saved as
   aliases), or new with an optional `parent` / `sister_of` (→ `entity_links`), gated
   at `resolution.min_confidence` 0.8.
3. **Tier 3, new:** create the entity, log it in `unresolved_entities`.

### 2. Root causes

| # | Cause | Example |
|---|---|---|
| RC1 | Tier 1 saves the raw mention as an alias with no check. Extraction often rewrites a descriptor or another org to a known entity, and that rewrite becomes a permanent rule. | "China's leading logistics company" → `resolved` "China" → alias of China |
| RC2 | Tier-1 alias hits are trusted forever, including legacy fuzzy-era aliases. | "SEER Robotics" alias on Serve Robotics; "Samsung SDS" on Samsung SDI |
| RC3 | Generic descriptors are extracted as entities (tier 3) or folded into a real one (RC1). Spelling variants of the same descriptor become separate entities or aliases. | "Customers", "Investors", "leading logistics companies in China" vs "Chinese leading logistic company" |
| RC4 | The alias store keeps case/format variants as separate rows (matching already ignores case). | "Humanoid robots" + "humanoid robots" |
| RC5 | Symmetric types can be stored in both directions. The extraction prompt says to alphabetize, but nothing enforces it, and `direction.dedupe_rows` keys on ordered (a, rel, b). | Intel/NVIDIA competes_with both ways (ROB-082226-003) |

### 3. Design

#### 3.1 Name keys (new `src/names.py`, deterministic, free)
- `strict_key(s)`: NFKC → lowercase → strip punctuation → collapse spaces → drop legal
  suffixes (inc, corp, corporation, co, ltd, limited, plc, llc, gmbh, ag, sa, holdings).
  Keeps word order. Answers "is this the same written name?"
- `loose_key(s)`: strict_key → singularize simple plurals → drop filler words
  (the, of, in, a, an, and, for, its, 's) → demonym → place (small table:
  Chinese→china, Japanese→japan, Korean/South Korean→south korea, American/U.S./US→united
  states, European→europe, German→germany, …) → sort tokens.
  "leading logistics companies in China" and "Chinese leading logistic company" both become
  `china company leading logistic`.
- **Safety rule:** `loose_key` must never auto-merge two *named* organizations
  ("Bank of China" vs "China Bank" collide). It is used only (a) for generic phrases
  (3.2), and (b) to add candidates to the LLM call.

#### 3.2 Generic-descriptor gate (after the exact canonical / trusted-alias lookup of the mention)
- **As built:** word lists alone can't tell "China Telecom" from "Chinese telecom company", so the *original
  casing* decides. Every word must be generic or a place, AND one generic word is lowercase or plural/collective
  (sector, industry, market…). Title-Case singular names stay names; government phrases go to normal lookup.
  Checked against every entity name in prod (2026-11-09): 74 flagged, all real descriptors.
- A name is **generic** when, after `loose_key`, every token is in a generic
  vocabulary (company, firm, startup, maker, manufacturer, provider, supplier, customer,
  investor, operator, sector, industry, market, leading, major, top, global, domestic,
  logistics, shipping, telecom, automotive, robotics, humanoid, robot, ai, …) or is a place
  token. No proper-noun token remains.
- Generic names never become aliases of a real entity, and are never sent as `same_as` candidates.
- **Product decision (2026-11-09, supersedes the group-entity default below):** a generic phrase is NOT an entity.
  It is tagged only to a descriptor bucket an admin curated: a type `group` entity, one of its aliases, or a
  generic-named legacy entity with the same `loose_key`. Otherwise it is **discarded**: no entity is created, its
  edges are dropped, and it is logged in `unresolved_entities` with `suggested_type='generic_discarded'` for review.
  It is never matched to a real company: lowercase "humanoid" ≠ Humanoid.
- **Superseded (not built), kept for the record — auto-created group entities:** each distinct generic
  `loose_key` maps to ONE group entity (type `group`), whose display name is the first
  phrasing seen. When a place token is present, it is auto-linked `subsidiary_of` that
  place's government entity. So "leading logistics companies in China" and "Chinese leading
  logistic company" land on the same group, a child of China.
  - Alternative: drop edges with a generic endpoint, logged to `unresolved_entities`
    with reason `generic`. **Decision needed.**
- Government phrases ("Chinese government", "Government of China", "Chinese state",
  "Beijing" as government) map deterministically to the country's government entity.
  That is the one generic-looking class allowed as an alias.

#### 3.3 Matching order (per extracted name, after the gate)
1. **Canonical name**, exact or `strict_key`, vs `entities.name` → trusted (tier 1).
2. **Verified alias**, exact or `strict_key` → trusted (tier 1).
3. **Unverified alias hit** → NOT trusted. It goes to the existing per-card LLM call as a
   name with that entity (plus family) as candidates. same_as ≥ 0.8 → use it and mark
   the alias verified. Otherwise new or child per the LLM, and the alias is flagged for
   admin review (not deleted automatically). As built: the contradiction is appended to
   `Decision.review`, so `flag_unsure` writes an admin attention entry.
4. Group-entity `loose_key` match (generic names only).
5. Everything else → the existing tier-2 LLM. Candidates = today's fuzzy list +
   `loose_key` hits.

#### 3.4 Alias writes (fixes RC1, RC4)
- Save a mention as an alias ONLY when the LLM said same_as ≥ 0.8 for it on this card
  (as built: tier-1 matches write no alias; format variants already match by `strict_key`).
  An extraction rewrite (`resolved` ≠ `mention`) is not evidence by itself. In that case
  the mention goes through 3.3 step 3/5 like any other name.
- `insert_alias` skips a new row whose `case_key` (case/punctuation/spacing only) equals an
  existing alias or the name on the same entity. Legal-suffix variants ("Schaeffler" on
  Schaeffler AG) are still stored, because consumers match the literal alias strings.
- New columns on `entity_aliases`: `verified BOOLEAN`, `source VARCHAR` (ingest_llm |
  ingest_new | generic | admin; NULL = legacy). No `created_at`. Admin alias writes,
  reverts included, are verified/admin.

#### 3.5 Children / parents (the "path back" rule)
- Keep the LLM's `parent` / `sister_of` output (already creates the new entity + link).
- Prompt addition: state-owned enterprises, agencies and subsidiaries are NEW entities with
  `parent` = the owner (e.g. China Telecom → parent China). They are never same_as.
  As built this is a prompt rule only, with no code guard (same pattern as the rest of tier 2).

#### 3.6 Symmetric edges (fixes RC5)
- At write time, order symmetric endpoints (lower entity_id first). `dedupe_rows`
  keys symmetric types order-free, per card only. Directional types and cross-card
  edges are untouched.

#### 3.7 New entities must be named in the card's own text (2026-11-09)
- A brand-NEW entity (tier 3, including a contradicted legacy alias) is created only if its resolved name or
  mention appears, case-insensitive and as whole words (also without a legal suffix: "Tesla Inc." → "Tesla"), in
  the card's headline + story + the research text the extraction saw (`ExtractionResult.context_text`).
- Otherwise there's no entity and its edges are dropped. It's logged as `suggested_type='ungrounded_discarded'`.
  This stops speculation-only names ("Traditional Automotive OEMs") from becoming entities.
- Matches to EXISTING entities (names, aliases, LLM same_as) are never affected. Inferred/speculative edges between
  known companies stay, labeled by `evidence_type`, for consumers to filter.
- Deterministic string match, no extra LLM call.

#### 3.8 Relationship evidence bars (2026-11-09)
- Every vocab type declares `min_evidence` and a `fallback` in `config/relationship_vocab.yaml`. Levels:
  speculative < inferred (= cited: web_grounded / inferred with source_refs) < direct.
  - **Fact claims**: acquires, invests_in, supplies, deploys, pilots, displaces, partners_with, integrates_with,
    built_on, hires_from, spins_out_from, regulates, litigates_against. These need ≥ inferred.
  - **Soft relations**: competes_with, benchmarks_against. These may be speculative.
- Fallback below the bar: displaces → competes_with; every other fact claim → drop. One general check
  (`extract.evidence_bar`) applies it to every type before the direction check. Counts are in
  `enrichment.evidence_bar`, and a downgraded edge keeps `evidence_fallback_from`.
- The prompt states each fact type's bar, so the model chooses correctly up front. `displaces` is redefined:
  a stated or cited replacement at a named customer/market; mere entry/threat → competes_with; insourcing a part
  is not displaces.
- Real displacement arrives as direct/cited evidence on a later card (edge lifecycle: threat first as
  competes_with, the displacement when it happens).
- Verified on ROB-080126-003: "XPeng displaces NVIDIA" (9/10 runs across 3.6/3.8/Argon before) → "NVIDIA
  competes_with XPeng" (2/2 runs).
- Next (not built): Pass 3 also judges "type too strong for the evidence".

#### 3.9 The extraction labels every entity (2026-11-10)
- `EntityOut.kind` (required, response schema): `organization` = one specific named org; `descriptor` = a
  category described in words ("humanoid robots", "startups backed by SoftBank"). The prompt says to prefer the
  named org behind a descriptor and to omit products/chips/models.
- `descriptor` → the generic path: a curated group bucket, else discarded. The only override is an EXACT,
  case-sensitive existing canonical name / verified alias (a model mislabel of "Figure"). It's never sent to the
  resolution LLM.
- The §3.2 word gate stays underneath as the safety net. The model decides; code verifies.
- Live check on ROB-080126-003, ROB-090826-003 and ROB-032926-001: only named orgs emitted (test outputs not kept).
  The descriptors and products those cards used to produce were gone.

### 4. Cost
No new LLM calls: unverified-alias names and rewritten mentions ride the existing
one-call-per-card `decide_entities` request (slightly longer prompts). Name keys and the
generic gate are pure Python.

### 5. Migration (one-time, report → review → apply, same as entity-apply)
1. Add the alias columns. Legacy aliases start `verified=false, source=legacy`, except those
   passing safe rules (strict_key equal to the name, ticker, initials of the name) → verified.
2. Report the case/format-duplicate aliases → delete the redundant rows.
3. Report existing entities whose name is generic → group by `loose_key` → propose
   merges into one group entity per key + a place-parent link. Admin approves.
4. Symmetric reverse pairs → reject the duplicate (2 live today).

### 6. Rollout
- `resolution.shadow: true` for 3 daily ingests: compute the new decisions, log them
  (`resolution_shadow` events: would-alias, would-group, unverified-alias-sent, generic),
  and change nothing. Compare against what was written.
- Then switch on. Per-ingest counters: tier1_name, tier1_alias_verified, alias_checked,
  llm_same_as, llm_new, llm_child, generic_grouped, alias_writes_skipped.

### 7. Tests (unit, no LLM)
- strict/loose keys: case, punctuation, suffixes, plurals, demonyms, order. "Bank of China"
  ≠ "China Bank" under the merge rules.
- Generic gate: the two logistics phrasings → same group; "China Telecom" not generic.
- Unverified alias → routed to the LLM list (fake client), not trusted.
- Alias write refused for a rewritten mention without same_as; allowed for a format variant.
- insert_alias skips case variants.
- Symmetric write order + order-free dedupe; directional pairs kept.

### 8. Open decisions (product owner)
1. ~~Generic descriptors: group entities vs drop the edge.~~ **Decided 2026-11-09:** tag to a curated `group`
   bucket if one matches, else discard (see §3.2).
2. Demonym/place table scope: start with the countries/regions present in the data.
3. Legacy alias trust: unverified until checked (recommended) vs verified unless flagged.

---

<a id="ticket-internal-events"></a>
## Internal events (self-loop edges kept, shown only as a node-panel row)

_(was `#ticket-internal-events`)_


Status: built 2026-11-11 · Item 1 of the 2026-11-11 delivery list

### 1. Why
A self-loop `Tesla --deploys--> Tesla` is real news when it comes from "Tesla deploys Optimus in its own
factory": the product (Optimus, AI5) was folded into its company by a merge or an alias, and the event now
sits on one entity. Today merge **rejects** such rows and ingest **skips** them, so the event is lost from the
company's history. We keep them — without drawing them as edges or changing any between-entity count.

### 2. Design
An internal event is a self-loop row with the new status **`internal`**. Every live view, count and
consumer already selects `status IN ('active','materialized')`, so internal rows are excluded everywhere
by construction; the `status <> 'rejected'` COUNT queries (export stats, social picker) now also exclude
`internal`. Other `<> 'rejected'` readers deliberately still see internal rows, since they are real events
about the entity: card chips (export card entities, render chips), LLM/handle context (entities.py
evidence + audit context, handle_sweep context, dev-utils/entity_audit). None counts or draws edges.
Only symmetric (two-party) types are never internal: "X partners_with X" is noise.

### 3. Requirements
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

### 3a. Review follow-up (4ae9b6e review)
- An `internal` row can't be repointed (it would leave a non-loop `internal` row no view shows).
- A symmetric type can't be set `internal` by admin (matches §2).
- Merge keeps ONE internal event per (card, type): two products folded into one company on one card
  ("deploys Optimus", "deploys AI5") keep the most confident row; restatements are deleted, like dedupe_rows.
- Export: `internal_events: []` is on every card, so the first export after deploy rewrites every card doc
  once (together with `rel_subtype` and `event_type`); a card whose only row for an entity is internal
  still lists that entity as a chip (it is on the card).

### 4. Data step after deploy
Set Tesla's 10 self-loops that the 2026-11-10 Optimus/AI5 merges set `rejected` (all directional:
deploys ×5, pilots, integrates_with, built_on, supplies, spins_out_from) to `internal` via the admin
`set_edge_status` path (audited, revertible), then refresh those cards.

### 5. Consumers
Firestore card docs are safe: `internal` never appears in `relationships[]`; card docs only gain
`internal_events`. UNVERIFIED (CI audit W5, tracked in implementation_spec): if Traction Engine reads the
DuckDB `relationships` table directly with a `status <> 'rejected'` filter (not `IN ('active',
'materialized')`), internal self-loops would reach it as edges — confirm its filter before the Tesla
data step.

### 6. Tests
`make test-ui`: the node-panel row shows the right count and its click opens exactly those cards.
tests/test_internal_events.py — merge (directional → internal, symmetric → rejected, dead rows not
revived), ingest rule, card export (relationships + stats identical, internal_events filled once, graph has
no self-loop edge), admin status guard + revert.

---

<a id="ticket-relationship-subtypes"></a>
## Relationship subtypes (`rel_subtype`)

_(was `#ticket-relationship-subtypes`)_


Status: built 2026-11-11 · Owner: CKG Robotics · Consumers: Traction Engine, site, admin

### 1. Why
The 15 parent `rel_type`s are too coarse for Traction Engine replies ("supplies" can be contract manufacturing,
IP licensing or a reseller deal; "competes_with" can be a named rival or one line of inferred pressure). We want
that finer meaning **without** asking the LLM to decide more and **without** changing any existing count.

### 2. Rules of the change (hard requirements)
- R1 `rel_type` is unchanged everywhere — values, vocabulary, extraction prompt, response schema, counts.
- R2 `rel_subtype` is an **additive, nullable** column on `relationships`. NULL = plain parent.
- R3 A subtype always belongs to exactly one parent (`subtypes.SUBTYPES`); it rolls up, so recap, graph
  "seen in N", node-panel top 5, card PNGs and Traction Engine filters on `rel_type` are unaffected.
- R4 Deterministic — `src/subtypes.classify()` reads only what the row already has: `rel_type`, `mechanism`,
  `evidence_type`, endpoint names, source entity type. No LLM, no network.
- R5 Precision over coverage: no explicit textual signal → NULL. Direction-sensitive subtypes need the
  source to be the actor (named before the manufacturing verb/role) or they stay NULL.
- R6 Always consistent with the row: recomputed whenever a card's edges change, and fully at every ingest run.

### 3. Vocabulary (parent → subtypes)
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

### 4. Where it is computed
- `subtypes.refresh(con, catalyst_ids=None)` — recompute, write only changed rows (one set-based UPDATE).
- `hashtags.store_for_catalyst()` calls `refresh(con, [cid])` — the existing per-card chokepoint every
  edge-changing path already calls (ingest write, every admin edit and revert, entity merge).
- `ingest.run` calls `refresh(con)` after `hashtags.backfill` — the first prod run after deploy **is the
  backfill** (~3.6k rows, ~12 s once); later runs are a 0.3 s no-op unless rules change (self-heal).
- Migration: `ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_subtype VARCHAR` in `db.init_schema`
  and `admin_ops.ensure_schema` (the admin can write before an ingest has migrated). Verified on a prod copy.

### 5. Where it is exported
- Card docs (`catalysts/items/*.relationships[]`) and `cards.json`: new key `rel_subtype` (null when none).
- Graph payload edges: new key `rel_subtype`.
- `export._load_cards` tolerates a DB not yet migrated (exports null).
- Not in embeddings (embed text unchanged → no re-embed), not on card PNGs (no re-render), not in the UI yet.
- Effect of the first prod run: every card doc with at least one relationship gets one Firestore rewrite
  (the new `rel_subtype` key, null included, changes each card's hash) — one-time, then delta as usual.

### 6. Validation (data/subtype_rules_eval_20261111.txt)
All 8,567 live edges classified. Hand-read ~140 random samples over four tuning rounds; final precision
14/14 manufactures_for, 10/10 licenses_to, 10/11 sells_through, 14/14 funds, 14/14 owns, 14/14 joint_venture,
10/10 research_collaboration, ~13/14 co_development, 12/12 rivals, 12/12 pressures. Blind Gemini
cross-check (picks a subtype itself, never sees the rules' answer): 22/24 agree; both diffs are speculative
edges, where certainty is already carried by `evidence_type`.

### 7. Tests
`tests/test_subtypes.py` — rules (incl. every false positive found while tuning: "subsidiary", corporate
"Innovation Fund", "government-backed company", "manufacturers"/catalogue supplier, target-is-maker,
"distributed computing", "distributing … across its group", logistics, "parent company of <other>"),
roll-up, refresh idempotence, rel_type/status untouched, bulk path, per-card hook, admin repoint + revert.

### 7a. Review follow-up (d08d856 review)
Role attribution ("A supplies B, its contract manufacturer" → none), warehouse "distribution centers",
the verb "grants", "owned by / subsidiary of" must name the source, case-insensitive "joint venture",
blank names, generic first words ("General", "Advanced"…) never stand for a company; edit_entity (+ revert)
refreshes subtypes on all of the entity's rows, not only live ones.

### 8. Not in scope
UI display/filter of subtypes; subtypes for the other 11 parents; any LLM involvement.

---

<a id="ticket-render-fingerprint"></a>
## render fingerprint — know exactly which card images are out of date

_(was `#ticket-render-fingerprint`)_


**Status:** built 2026-11-19 (awaiting review + deploy).

### Problem
`make render-stale` compared "Firestore doc changed" with "image rendered". Any export that rewrites docs (adding a
field, a full sync) flags every card — 893 of 948 on 2026-11-19, almost all false. There was no way to tie a data
change (merge, rename, edge added or dropped, evidence change) to the exact images it affects.

### Design
- `src/render_fp.py`: `fingerprint(card)` = `v<FP_VERSION>-<16 hex>`, a sha256 of exactly what the images draw:
  headline, date, subtitle, story flag, every live between-entity edge (from, to, rel, confidence to 2 dp, evidence,
  status, in_story) and every entity (name, ticker, type, in_story). The order doesn't matter. It ignores fields
  the images never draw: hashtags, event_type, subtypes, mechanism, internal events, share text.
- Export writes it into every card doc as `render_fp`.
- The render service stamps the doc's `render_fp` onto each uploaded image's metadata: `cards/<id>.png` and
  `graphs/<id>.png`. It does this on a fresh upload and on the "unchanged bytes" metadata patch.
- `render-stale` lists a card when an image is missing, unstamped ("not fingerprinted"), or stamped with a different
  hash ("content changed"). Docs exported before this change fall back to the old time check.
- When a card or poster TEMPLATE changes what's drawn, bump `FP_VERSION`: every card then re-renders once, on purpose.

### Rollout
Deploy the backend (export) and the render service. The next export writes `render_fp` to every doc, a one-time
full doc rewrite. Then run one `make render-stale APPLY=1`, which renders and stamps every image once. From then on
it lists only cards whose drawing really changed.

### Known limit
The card PNG is drawn from DuckDB and the poster from the Firestore doc. Both come from the same export, so they
agree in normal operation. The stamp is the doc's hash at render time.
