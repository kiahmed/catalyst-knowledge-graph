# Implementation Spec — Robotics module (by Arboryx)

*Phase-by-phase build plan. Each phase has a gate — don't start the next phase until the gate is passed. Reflects the post-Phase-0 reality (2026-04-17): Arboryx upstream hardened, data corrected, deck grid UI prototyped. We are building the product, not validating with manual posts.*

---

## Phase 0 — Validate & harden foundation  ✓ COMPLETE

**Goal:** Confirm enough historical volume to work with, establish the card visual language, and fix every upstream data-quality issue discovered along the way so Phase 1 builds on solid ground.

| # | Ticket | Status | Output |
|---|---|---|---|
| 0.1 | Audit historical volume | ✓ Done | 178 Robotics entries over 35 days (~5/day). Rich entity density. Volume sufficient. |
| 0.2 | Card templates + deck prototype | ✓ Done | `templates/cards/{card.html, base.css, preview.html, deck.html, deck_grid.html}`. Grid variant wins for Phase 1 frontend. |
| 0.3 | Master log corrector utility | ✓ Done | `dev-utils/master_log_corrector.py` shipped + applied to GCS. 895 entries, 336 timestamps reformatted, 36 date-ranges collapsed, 32 sentiment-synonyms normalized, 895 unique IDs assigned. Idempotent on re-run. |
| 0.4 | Arboryx contract hardening | ✓ Done | Handoff: `docs/proposed_arboryx_changes.md`. Upstream shipped: (a) `append_to_memory_log` + GCS `if_generation_match` retry, (b) strategist `source_url` field, (c) `values.yaml:86` data-engineer sentiment constraints, (d) range rejection. Arboryx enhancements: URL-aware dedup (Layer-1 URL equality + TF-IDF/entity-overlap URL awareness, 9/9 tests), concurrency fix (`--max-instances=1 --concurrency=1` + cross-invocation overlap race fix, 9/9 tests in `test_append_race.py`). |
| 0.5 | Pivot from manual posting to built automation | ✓ Decision | Phase 0 originally gated on 3 weeks of manual social posting to validate engagement. Superseded: Postiz is deployed in sibling repo, so automation is cheaper than manual effort. Engagement validation moves into Phase 1 (shipping the product generates real posts, real metrics). |

**Gate:** ✓ passed — volume confirmed, data normalized, upstream contract locked. Proceed to Phase 1.

---

## Phase 1 — Ship the product (Robotics only)

**Duration:** 5 weeks
**Goal:** End-to-end pipeline live on a public URL, posting to socials daily, with a live knowledge-graph UI (colored nodes, edge tooltips, time + weight filters, invalidated-edge overlay) and insight detectors rendering as graph overlays. A real user can visit the site, explore the graph, open catalyst cards, and share them.

### Week 0 — Local infrastructure (one-shot, ~2 days) ✓ SCAFFOLDED

These shipped together as the infra lockdown (see `docs/infra_spec.md`). Left as tickets for audit/reference; all are already in the tree.

| # | Ticket | Description | Status |
|---|---|---|---|
| 0a | `docs/infra_spec.md` | Local Docker dev stack + GCP target architecture + per-tool deployment plan. Locks all infra decisions: Cloud Functions Gen 2 for ingest, Cloud Run for render, Cloud Run Job for social, Firebase Hosting for frontend, GCS-backed DuckDB file. | ✓ Written |
| 0b | `tools/` scaffolding | Six self-contained tools: `duckdb/`, `robotics-ingest/`, `robotics-render/`, `robotics-social/`, `frontend-deploy/`, `infra/`. Each has Dockerfile + main.py/init.sql/firebase.json + deploy.sh + README. | ✓ Scaffolded |
| 0c | Root `docker-compose.yml` + `Makefile` + `.env.example` + `.gitignore` | One command (`docker compose up`) boots the whole local stack. `make ingest / render / social / db / frontend` cover every developer action. | ✓ Ready |
| 0d | `tools/infra/` Terraform | 10 files: provider, bucket, artifact registry, per-tool SAs with least-privilege bindings, Secret Manager entries, Cloud Function, Cloud Run service, Cloud Run Job, Pub/Sub topic, Cloud Scheduler. Not `terraform apply`-ed yet — applies after Phase 1 gate. | ✓ Written, not applied |

**Gate:** `docker compose build && docker compose up -d && make ingest-dry` returns a valid JSON response end-to-end on a developer laptop. Arboryx GCS read works with the provided SA. Proceed to Week 1.

### Week 1 — Foundation, schemas, contracts

| # | Ticket | Description | Status |
|---|---|---|---|
| 1.1 | `src/` scaffolding | `src/{__init__,config,db,extract,resolve,ingest,export,fetch}.py` shipped 2026-04-18. `detect/render/social/lifecycle` deferred to their respective week. | ✓ code-complete |
| 1.2 | DuckDB schema wrapper (`src/db.py`) | Connection manager, idempotent schema (incl. new `ingestion_meta` compound watermark + `last_gcs_generation`), CRUD helpers, `delete_by_entry_id` for reextract. | ✓ code-complete — **blocked** on schema sync (see below) |
| 1.3 | Seed entity list | Curate `data/seed_entities.csv` with ~150-200 Robotics companies from ROBO/BOTZ ETF holdings, top Crunchbase robotics startups, and grep-mined frequent mentions in Arboryx's existing Robotics entries. Columns: `name, ticker, type, aliases (pipe-separated)`. Loader inserts into both `entities` and `entity_aliases`. | ◻ not started |
| 1.4 | Relationship vocabulary | `config/relationship_vocab.yaml` — 15 closed types with description + direction + examples. Consumed by 1.6 via `src.config.load_config()`. | ✓ done |
| 1.5 | JSON export contract (schema_version=1) | `src/export.py` emits `cards.json` with `schema_version`, per-card `share.{twitter_text,linkedin_text}`, stats, `graph_config` echo, flat `graph.{nodes,edges}` projection, `graph_insights: []` placeholder. | ✓ code-complete |

### Week 2 — Extraction pipeline

| # | Ticket | Description | Status |
|---|---|---|---|
| 1.6 | Extraction prompt + LLM call | `src/extract.py`: Gemini 2.5 Flash + Pydantic `response_schema` (strict structured output), closed-vocab validation, `reasoning` field for spot-check transparency, confidence partitioning (drop/flag/write bands from config). Sentiment copied verbatim from `sentiment_takeaways`, not re-derived. | ✓ code-complete — needs first real call to verify |
| 1.7 | Entity resolver | `src/resolve.py`: 3-tier (exact → `thefuzz.ratio ≥ 88` → new-entity + unresolved-staged, always returns entity_id so relationships always write). | ✓ code-complete |
| 1.8 | ~~Hand-label 30 findings~~ | **Superseded.** Per extraction-validation stance (memory): LLM is the labeler; validation is prompt craft + `response_schema` + backtest runner (`dev-utils/backtest.py`) + user spot-check. No Python hand-label harness. | ✗ dropped |
| 1.9 | ~~30-edge precision test~~ | **Superseded.** Week-to-week iteration is backtest eyeballing + prompt/schema tuning. Formal precision eval happens once at the 200-edge gate (ticket 1.26). | ✗ dropped |

### Week 3 — Ingest + frontend wired end-to-end

| # | Ticket | Description | Status |
|---|---|---|---|
| 1.10 | Firestore ingest + wiring | `src/ingest.py`: compound watermark `(last_processed_date, last_processed_entry_id)` drives a Firestore `start_after` cursor over Arboryx's `findings/{entry_id}` collection. Per-entry transactional write; watermark advance per row so mid-batch crash is safe. `tools/robotics-ingest/main.py` rewired to call it + `src.export`. | ✓ code-complete |
| 1.11 | ~~Backfill~~ | **Rolled into 1.10.** No separate command. On an empty DB the watermark is null, so `make ingest` naturally processes every Robotics finding oldest-first. `LIMIT=N` stages a bounded first run. | ✓ covered by 1.10 |
| 1.12 | ~~Incremental mode~~ | **Rolled into 1.10.** Same `src/ingest.py` path: cursor on `(date, entry_id)` + set-difference on `entry_id ∉ already-processed`. | ✓ covered by 1.10 |
| 1.13 | Production frontend (`frontend/index.html`) | Promote `templates/cards/deck_grid.html` to `frontend/index.html` per `technical_spec.md` §2.6. Copy `base.css` → `frontend/assets/app.css`. Extract share + fetch logic into `frontend/assets/app.js`. Replace hardcoded cards array with `fetch('cards.json')`. Add `schema_version` guard: refuse-to-render banner if mismatch. Card deeplinks via URL hash (`#card=<entry_id>`). | `make frontend` + browse to http://localhost:8000 shows real ingested data. All existing UI (pagination, search, confidence slider, date filter) still works. |
| 1.14 | Share feature | In `frontend/assets/app.js`: add share icon to compact view of each card (upper-right). On click: try `navigator.share()`; fallback to overlay with Twitter/LinkedIn/Reddit deeplinks + copy-link + download-PNG per `technical_spec.md` §2.7. Platform-specific share texts come from the already-exported `card.share.{twitter_text, linkedin_text}`. | Manual test: share overlay opens on card, deeplinks produce correctly-encoded URLs, `navigator.share` triggers on mobile, copy-link writes the correct hash URL to clipboard. |
| 1.15 | Share text generation in exporter | Extend `src/export.py`: second LLM pass per card produces `twitter_text` (≤ 280 chars, relevant cashtags) and `linkedin_text` (~500 chars, professional tone). Written into `card.share` in `cards.json`. | Every card in export has populated `share.twitter_text` and `share.linkedin_text`. Character-limit tests pass. |

### Week 3b — Graph UI surface (realize the export contract)

Today `frontend/index.html` fetches `cards.json` but only renders the card grid. The entire `graph.{nodes, edges}` projection, `graph_config` echo, per-edge `status` + `mechanism` + `source_refs`, and `graph_insights[]` placeholder are dropped on the floor. These tickets close that gap so the ingested data is actually *visible* as the product surface described in `technical_spec.md` §2.6.

| # | Ticket | Description | Done when |
|---|---|---|---|
| 1.13b | Graph canvas (core) | Add Cytoscape.js via CDN to `frontend/index.html`. New `/graph` view toggle alongside the card grid. Render `payload.graph.nodes` as circles — size ∝ `edge_count`, fill = normalized `heat` on a cool→warm gradient. Render `payload.graph.edges` — width ∝ `confidence`; `status='invalidated'` → dashed + muted; color by `evidence_type` (direct solid, web_grounded green, inferred amber, speculative red dashed). Hover an edge → tooltip with `mechanism`, `evidence_type`, `confidence`, `source_refs` count. | `/graph` renders real ingested payload end-to-end. Node sizes + heat colors match expected weighting. Edge styling differentiates all four `evidence_type`s visibly. |
| 1.13c | Graph filters from `graph_config` | Consume `payload.graph_config` and wire three UI controls to the live graph: time window (slider/presets), min edge confidence (slider), show-invalidated (toggle). Filter client-side on the already-loaded payload — no server round-trip. URL query-string persists state (`?tw=90&minc=0.3&inv=1`) for shareable filtered views. | Changing any control updates the graph within one frame. Reload with a query-string restores the exact view. Toggle-invalidated-on surfaces dashed historical edges; toggle-off hides them. |
| 1.13d | Node + edge detail panels | Click a node → side panel: entity name/ticker/type + top-5 edges touching it (sorted by confidence) + one-click deeplink per edge (`#card-<entry_id>`). Click an edge → pinned tooltip with `mechanism`, `mechanism_strength`, `impact_magnitude`, indexed `source_refs` (resolved into `research_sources` URLs), catalyst date, and "open catalyst" button that expands the card overlay. | Clicking any node or edge surfaces correct metadata pulled from `payload.graph` + `payload.cards`. Deeplinks round-trip cleanly (graph → card → back). `source_refs` indices render as live links to the citation URLs. |
| 1.13e | Card ↔ graph bidirectional nav | Expanded card view grows a mini-graph: that card's entities plus their other in-window edges (local subgraph slice), reusing the Cytoscape instance. Reverse direction: clicking a graph node surfaces the top-3 most-confident cards mentioning that entity in a side list. | Every expanded card shows a contextual subgraph. Every graph node click surfaces relevant cards. Round-trip card → subgraph → click other node → new cards works without a page reload. |
| 1.13f | Schema-version + empty-state guards | Refuse-to-render banner if `payload.schema_version !== 1` (called out in 1.13 but not yet implemented). Graceful "no catalysts yet" skeleton for empty `cards[]` / empty `graph.nodes`. Loading spinner until `fetch('cards.json')` resolves. | Corrupted / mismatched schema shows the banner instead of a blank screen. Empty-DB state shows a friendly "run `make ingest` to populate" hint. No flash-of-unstyled-content on slow network. |

**Dependency chain:** 1.13b → 1.13c → 1.13f (unblock the graph view) → 1.13d → 1.13e (polish). 1.21 still waits on 1.19/1.20 detectors and then consumes `graph_insights[]` on top of this canvas.

### Week 4 — Social automation + insight detectors (graph overlays)

| # | Ticket | Description | Done when |
|---|---|---|---|
| 1.16 | Social card PNG renderer | Implement `src/render.py` (Jinja2 + Playwright screenshot at 1200×630 → `/data/exports/card_images/{entry_id}.png`). **Wire into `tools/robotics-render/main.py`** — the scaffolded Flask endpoints already call the rendering primitive; this ticket finalizes the primitive and removes the stub behavior. Idempotent: skip if PNG exists and DuckDB row unchanged. | `make render CARD_ID=ROB-041726-001` produces a correct-aspect PNG. `make render-batch` renders every card in the last 30d. |
| 1.17 | Postiz API client | Implement `src/social.py`: `httpx` client for Postiz `/upload`, `/posts`, `/analytics/{id}`. Config from `config.yaml` + env. **Replace the stub in `tools/robotics-social/main.py`** with real uploads/schedules. Uses `social_posts` DuckDB table already in the schema. | `make social-dry` shows the candidate queue. `make social` posts to a test Postiz channel and records post IDs. |
| 1.18 | Postiz daily post scheduling + analytics | Extend `src/social.py`: 24h-after-post analytics pull → update `social_posts.{impressions,engagements,clicks}`. Add a second Cloud Run Job target in `tools/infra/cloud_run.tf` for `robotics-social-analytics`. `scripts/daily_run.py` ties the flow together: ingest → render → social → analytics-pull. | Daily run in dev-mode posts 2-3 cards and pulls analytics for yesterday's posts. |
| 1.19 | Chokepoint detector | Implement in `src/detect.py`: query top entities by in-degree growth (per `technical_spec.md` §2.4 insight queries). Emit JSON consumable by `cards.json` `graph_insights[]`. | Returns ranked list with growth ratios. Unit test on seeded fixture. |
| 1.20 | Narrative velocity + cluster-break | Same module: relationship-type 30-day acceleration query; new-co-occurrence detection query. Results feed `graph_insights[]`. | Both return ranked results. Tests on fixtures. |
| 1.21 | Graph-insight overlay wiring | `src/export.py` reads detector outputs (1.19 + 1.20) and populates `cards.json.graph_insights[]`. Frontend renders them as highlight overlays on the graph (e.g. halo the chokepoint node, badge accelerating rel-types in the edge legend). **Replaces the old markdown-digest ticket.** | `cards.json` contains at least one chokepoint + one velocity insight after an ingest run. UI shows the overlay panel with clickable highlights that re-zoom the graph. |

### Week 5 — Deployment, evaluation, gate

| # | Ticket | Description | Done when |
|---|---|---|---|
| 1.22 | Daily run orchestrator | `scripts/daily_run.py` sequence: `curl robotics-ingest` → `curl robotics-render /render-batch` → `curl robotics-social /run` → `make deploy-frontend`. Single entrypoint for local dev-mode; Phase 2 replaces with Cloud Scheduler + Pub/Sub fan-out (already in `tools/infra/`). | `python scripts/daily_run.py` completes cleanly end-to-end against the local stack. Second run is idempotent. |
| 1.23 | Deploy to GCP + Firebase Hosting | First real prod push. Order: (a) `make deploy-infra` (Terraform apply — creates buckets, SAs, registries), (b) `make deploy-ingest / deploy-render / deploy-social` (builds containers, pushes to Artifact Registry, deploys), (c) `make deploy-frontend` (Firebase). Enable Cloud Scheduler cron. | Public URL serves the live site. Daily cron triggers ingest at 06:00 UTC, render + social at 07:00. Error budget tracked in workbench. |
| 1.24 | Unresolved entity review CLI | `scripts/review_unresolved.py`: terminal UI — list unresolved mentions with context, let operator map to existing entity / create new / discard. Writes resolved edges into `relationships`. | Operator can clear the queue to zero in a session. |
| 1.25 | Hand-label 200 edges | Expand test set from 30 to 200 labeled edges spanning multiple relationship types. **Schema per label:** `{catalyst_id, from, to, text_span, gold: {rel, in_vocab: bool, direction_ok: bool}, no_fit_hint?: string}`. Annotators flag out-of-vocab edges (`in_vocab=false`) with a short `no_fit_hint` (e.g. `"uses_hardware"`, `"co_develops"`) rather than forcing a fit into the closed 15-type vocab. | `tests/fixtures/labeled_200.json` committed. ≥5% of the set is deliberately sampled from edges that sit awkwardly in the current vocab (see workbench 2026-04-18 closed-vocab note). |
| 1.26 | Phase 1 precision + vocab-gap re-eval | Run extraction on the 200-edge set. Implement `src/eval.py:score(labels, preds)` returning `{precision, direction_accuracy, vocab_gap_rate, top_gap_hints, passes}`. Precision is computed over `in_vocab=true` labels only (exact match on `rel` AND `from`/`to` orientation). `vocab_gap_rate = len(no_fit) / len(labels)`. **Compound gate:** `precision ≥ 0.85 AND direction_accuracy ≥ 0.90 AND vocab_gap_rate ≤ 0.10`. If vocab-gap exceeds 10%, expand the vocab (adding the top `no_fit_hint` types to `ExtractionSchema`) **before** iterating the prompt for precision — otherwise you're optimizing inside a coordinate system that doesn't fit the data. | `pytest tests/test_extract.py -k large_set` passes. `src/eval.py` report written to `docs/eval_runs/<date>.json` with the three metrics + top 5 gap hints. |
| 1.27 | Phase 1 gate assessment | Review: does the site have real users? Is Postiz posting generating any engagement? Are detectors producing ≥2 novel insights/week? Extraction precision ≥ 85%? | Gate decision written to `docs/workbench.md`. |
| 1.28 | Crawler access: robots.txt (tech spec §2.7a) | Requested by soljet-postiz: X/LinkedIn crawlers fetch `/card/<id>` + `/card-img/<id>.png` for link previews; the SPA `**` rewrite answering `/robots.txt` with HTML makes some crawlers treat the site as disallowed. Ship `frontend/robots.txt` (`User-agent: *` / `Allow: /`) — a real static file wins over Hosting rewrites, so no firebase.json change. Ships with the normal `make deploy-frontend`. | `curl https://robotics.arboryx.ai/robots.txt` returns 200 `text/plain` with `Allow: /` (not the SPA HTML). X card validator renders a preview image for a `/card/<id>` URL. |

**Gate:**
- Extraction precision ≥85% on the in-vocab portion of the 200-edge set.
- Direction accuracy ≥90% on in-vocab edges (orientation is load-bearing for a closed-vocab graph — see workbench 2026-04-18).
- Vocab-gap rate ≤10% on the 200-edge set. If exceeded, expand the 15-type vocab with the top `no_fit_hint` categories **before** re-tuning for precision; otherwise precision is measured in the wrong coordinate system.
- ≥2 novel relationships/week surfaced by detectors.
- Site publicly reachable, Postiz posting daily.
- Organic shares measurable (>0 shares/week attributable to the share button).

---

## Phase 2 — Prove the product (outline only)

*Do not detail until Phase 1 gate passes.*

| Ticket area | Description |
|---|---|
| Edge lifecycle detector | Event-driven materialize/invalidate detector per `technical_spec.md` §2.6a. New edge on an `(entity_a, entity_b)` pair triggers LLM validation over existing edges on that pair; confirm → boost weight + `status='materialized'`; invalidate → penalty + `status='invalidated'`. Knobs in `cfg.lifecycle`. |
| Sentiment-weighted entity scoring | Today `sentiment_label` is display-only. Promote it to a scoring signal: (a) add `sentiment_target_entity` to the extraction schema so the LLM names which entity the finding's sentiment attaches to; (b) use relationship-vocab polarity (collaborative vs adversarial vs neutral) to distribute sentiment across edge endpoints when target is ambiguous. Entity score = weighted sum of edge-confidence heat + Σ(sentiment_polarity × catalyst_confidence). Entries lacking a `Sentiment:` marker contribute edge-confidence only — clean skip, no hallucinated attribution. Backtest against the 30-edge precision gate (1.26) before shipping to UI. |
| Track record scoring | 30/60/90-day stock price pull for related tickers. Score catalyst accuracy. Publish per-entity track record overlays in the graph UI. |
| Subscriber list | Email capture + notification on high-significance catalysts (SES or Resend). Magic-link auth only. |
| Engagement analytics loop | Join Postiz `/analytics` back into DuckDB. A/B test share copy. Feedback loop into which card types to prioritize. |
| Multi-post packaging | Threads vs single cards vs carousels. Postiz supports this; pick per card-type. |

**Gate:** Returning users >30% week-over-week. Subscriber waitlist ≥100.

---

## Phase 3 — Expansion (outline only)

| Ticket area | Description |
|---|---|
| Second sector | Turn on AI Stack or Space & Defense. Same pipeline, same DuckDB. Sector toggle in frontend. |
| Pro tier | Stripe. Free = delayed + 1 sector. Pro = real-time + all sectors + API. |
| Public API | FastAPI endpoints over DuckDB views. Rate-limited by tier. |
| Entity-overlap dedup edge case | Address the deferred Arboryx dedup issue (noted in `docs/proposed_arboryx_changes.md`) if it becomes material at scale. |

**Gate:** Paid conversions from free users.

---

## Phase 4 — Platform (outline only)

| Ticket area | Description |
|---|---|
| Multi-sector | All 6 Arboryx sectors live. |
| Contribution layer | Upvote, flag, add context to catalysts. |
| Agent marketplace | Third-party agents publish to the platform. Revenue share. |

**Gate:** Revenue covers infra costs.

---

## Dependency graph (Phase 1)

```
 1.1 scaffolding
    │
    ├──▶ 1.2 DuckDB schema ──────────────┐
    │                                    │
    ├──▶ 1.3 seed entities ──┐           │
    │                        │           │
    ├──▶ 1.4 vocab YAML ─────┼──▶ 1.6 extraction ──▶ 1.7 resolver
    │                        │                           │
    └──▶ 1.5 JSON contract ──┘                           │
                 │                                       │
                 │       1.8 label 30 ──▶ 1.9 precision test
                 │                              │
                 │                              ▼
                 │                      1.10 ingest (covers first-ingest staging + incremental)
                 │                                                              │
                 ├──────────────────────────────────────────────────────────────┤
                 ▼                                                              ▼
         1.13 frontend ──▶ 1.14 share ──▶ 1.15 share-text export   1.19-1.20 detectors
                 │                                │                        │
                 │                                ▼                        ▼
                 │                          1.16 PNG render          1.21 graph insight overlay
                 │                                │
                 │                                ▼
                 │                          1.17 Postiz client ──▶ 1.18 daily posting
                 │                                                        │
                 └────────────────────────────────────────────────────────┤
                                                                          ▼
                                                         1.22 daily_run ──▶ 1.23 deploy
                                                                          │
                                                                          ▼
                                                                1.24 unresolved CLI
                                                                          │
                                                                          ▼
                                                            1.25-1.26 eval ──▶ 1.27 gate
```

**Critical path:** `1.1 → 1.2 → 1.6 → 1.10 → 1.13 → 1.22 → 1.23`

**Current position (2026-05-10):** 1.1/1.2/1.4/1.5/1.6/1.7/1.10 code-complete; source cutover to Arboryx Firestore landed (see workbench 2026-05-10). Next: `make firestore-ping` → `make ingest LIMIT=5` → eyeball → drain.

Everything off the critical path (share text, PNG, Postiz, detectors, graph-insight overlay) is necessary for the gate but can be parallelized once the critical path reaches end-to-end data flow.

---

## Reuse from Phase 0 and Week 0

| Artifact | Reused in Phase 1 ticket |
|---|---|
| `dev-utils/master_log_corrector.py` `firestore_download` helper | 1.10 ingest (sanity checker; write-back retired post-Firestore cutover) |
| `dev-utils/master_log_corrector.py` `entry_id_for` + `normalize_timestamp` | 1.6 extraction (entry_id is the canonical key now) |
| `templates/cards/card.html` + `base.css` | 1.13 frontend + 1.16 PNG renderer |
| `templates/cards/deck_grid.html` | 1.13 frontend (source for `index.html`) |
| ~178-entry Robotics corpus already in Arboryx Firestore | 1.10 first-ingest input |
| `docs/proposed_arboryx_changes.md` handoff | Reference — entry_id + source_url + sentiment shape all come from here |
| `tools/robotics-ingest/main.py` (HTTP + Firestore + watermark scaffolding) | 1.10 — fill in the src/ call sites |
| `tools/robotics-render/main.py` (Flask + Playwright scaffold) | 1.16 — finalize `src/render.py` primitive |
| `tools/robotics-social/main.py` (Postiz HTTP skeleton) | 1.17 — finalize `src/social.py` primitive |
| `tools/duckdb/init.sql` (full schema) | 1.1-1.2 — `src/db.py` re-uses the same SQL |
| `tools/infra/*.tf` (Terraform) | 1.23 deploy |
| `firebase.json` (repo root) | 1.23 deploy |

---

## Why this ordering

1. **Infra (Week 0) comes first** so every subsequent ticket has a container + deploy path. No retrofitting. See `docs/infra_spec.md`.
2. **JSON contract (1.5) precedes the extractor (1.6)** so frontend and backend can be built against the same contract in parallel. The contract is the decoupling seam.
3. **`src/` is a library, `tools/` wraps it.** This means src/ tickets (1.6-1.9) build testable functions first, then the tool wiring is a thin shim (one line per tool). Swapping Cloud Functions for Cloud Run later is a tool-level change.
4. **Extraction precision is gated on backtest spot-checks before the first real ingest (1.10)** so we don't pollute DuckDB with low-precision edges and have to delete them later.
5. **Frontend (1.13) ships before social automation (1.16-1.18)** because visible organic engagement on the site is more informative than social metrics. If the site feels wrong, we'd rather discover it before spending two tickets on Postiz.
6. **Detectors (1.19-1.20) sit late** because they're only meaningful once the graph has data. Running chokepoint on <50 catalysts is noise.
7. **GCP deploy (1.23) comes after the local stack is proven** because every deploy-ready bug is cheaper to find locally. Terraform applies once, daily deploys are `./deploy.sh` from that point.
8. **200-edge eval (1.25-1.26) is the final ticket before gate** so the gate decision is grounded in real precision numbers.
