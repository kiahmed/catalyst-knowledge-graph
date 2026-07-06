# Operating manual

> Coming back to this repo after weeks away? Start here.
> For *why* something is shaped the way it is, see `docs/technical_spec.md`,
> `docs/infra_spec.md`, and `docs/workbench.md` (decisions, dated).

## 1. What this thing is

A pipeline that turns Arboryx's market-research findings into a queryable
catalyst knowledge graph + social cards.

```
Arboryx Firestore (upstream)            ──┐
  findings/{entry_id}                     │
                                          ▼
robotics-ingest  ──extract→  DuckDB  ──export→  cards.json (local)
                                                Firestore CKG-Robotics (prod)
                                          │
                              robotics-render  ──PNG→  card_images/
```

(robotics-social retired 2026-07-05 — social posting moved to soljet-postiz.)

Single sector today (Robotics). Local: docker-compose stack. Prod: Cloud
Functions Gen 2 + Cloud Run + Firebase Hosting.

## 2. Directory tour

| Path | What you find here |
|---|---|
| `src/` | Pure-Python library: `ingest.py`, `extract.py`, `resolve.py`, `export.py`, `firestore_export.py`, `duckdb_sync.py`, `db.py`, `config.py`. **Image-baked** (COPY'd into the container). Edits require a rebuild. |
| `tools/robotics-ingest/` | HTTP wrapper around `src/ingest.py`. Cloud Function in prod. **Image-baked.** |
| `tools/robotics-render/` | Playwright + Chromium → card PNGs. Cloud Run service in prod. **Image-baked.** |
| `tools/robotics-social/` | (retired 2026-07-05 — social posting moved to soljet-postiz; see its `RETIRED.md`) |
| `tools/duckdb/` | Volume-owner container + `init.sql` (schema). |
| `tools/infra/` | Terraform — buckets, SAs, schedulers, Pub/Sub. Only matters at prod-flip time. |
| `tools/frontend-deploy/` | Firebase Hosting deploy script. Used at prod-flip time. |
| `dev-utils/` | One-off scripts: `master_log_corrector.py`, `reextract.py`, `backtest.py`, `firestore_indexes_bootstrap.sh`, `deploy.sh` (the engine behind `make deploy`). **Bind-mounted** (live edits, no rebuild). |
| `frontend/` | Static graph UI — `index.html`, `assets/`, the `firebase-config.js.example` stub. **Bind-mounted.** |
| `templates/cards/` | Jinja2 + CSS for card PNG rendering. **Bind-mounted.** |
| `config/config.yaml` | Non-secret runtime config. **Image-baked** — edits require a rebuild. |
| `data/` | Bind-mounted host folder: `robotics.duckdb`, `exports/cards.json`, `exports/card_images/`. Survives `make down`; wiped by `make nuke`. |
| `docs/` | Specs (technical_spec, infra_spec, implementation_spec, proposal_b). |
| `docs/workbench.md` | Decision log, newest-first. Read the top entry to find out what changed last. |
| `.env` | Local secrets + flags. **NOT in git.** Copy from `.env.example`. |
| `.env.prod` | Prod values + secrets — the single file `make deploy` reads. **NOT in git.** Copy from `.env.prod.example`. |
| `Makefile` | Every operational command. `make help` lists them. |
| `docker-compose.yml` | Local stack definition. Bind mounts + env passthrough live here. |
| `DEPLOYMENT.md` | Local + prod deploy runbook. |

### The "image-baked vs bind-mount" rule

Edit something under `src/` or `tools/robotics-{ingest,render}/`?
**You need to rebuild.** `make setup` (or `docker compose build`).

Edit something under `dev-utils/`, `frontend/`, `templates/`, `data/`?
**Live in the running container.** No rebuild needed.

Edit `config/config.yaml`? **Rebuild.** It's COPY'd into the image.

Edit `.env` or `docker-compose.yml`? **No rebuild — but recreate containers.**
`make down && make up` (env vars are captured at container create time).

## 3. Three places config lives — and what each is for

| File | Purpose | Reload after edit |
|---|---|---|
| `.env` | Secrets + per-environment toggles (API keys, feature flags, credential paths). One file, sourced by Makefile + docker-compose. | `make down && make up` |
| `config/config.yaml` | Defaults for non-secret runtime config (model name, confidence thresholds, time windows, schema versions). Env vars in `.env` override these. | `make setup` (rebuild) |
| `Makefile` | Operational commands. Reads `.env` via `-include .env` + `export`. | `make help` to list |

Read order when something looks wrong: `.env` → `config/config.yaml` → code in `src/`.

Prod has a fourth file: **`.env.prod`** — the prod analog of `.env`, read by
`make deploy` (not by the local stack). Same shape, prod values. See §8 and
`DEPLOYMENT.md`.

## 4. Critical env vars (local default → prod target)

The full set is in `.env.example`. These are the ones that actually change between local and prod:

| Var | Local | Prod | What it controls |
|---|---|---|---|
| `GCP_PROJECT` | `sample-gcp-project-id` | same | Arboryx Firestore project (where `findings/*` lives) |
| `FIRESTORE_DATABASE` | `(default)` | same | Same DB across env — collections separate the data |
| `FIRESTORE_COLLECTION` | `findings` | same | Source — Arboryx writes here |
| `FIRESTORE_EXPORT_ENABLED` | `false` | `true` | Whether to publish catalysts/graph to Firestore |
| `FIRESTORE_EXPORT_COLLECTION` | `CKG-Robotics` | same | Destination — this module writes here |
| `STORAGE_UPLOAD_ENABLED` | `false` | `true` | Whether render uploads PNGs to Firebase Storage |
| `STORAGE_BUCKET` | empty | `robotics-cards` | Firebase Storage bucket for public card PNGs |
| `DUCKDB_GCS_BUCKET` | empty | `robotics-data` | Set → ingest/render copy the DuckDB file to/from GCS per run. Empty → use the bind-mounted local file. |
| `FRONTEND_DATA_SOURCE` | `nginx` | `firestore` | Frontend read path (cards.json vs Firestore Web SDK) |
| `GOOGLE_APPLICATION_CREDENTIALS` | path to local SA key | (auto-attached by Cloud Run) | Auth |
| `GEMINI_API_KEY` | from `.env` | from Secret Manager | LLM auth |

The `*_ENABLED` flags are **off** locally so the local stack stays self-contained
(cards.json + nginx). Flipping them on locally would write to prod Firestore —
don't do this unless you mean to.

## 5. First-time local setup (cold start)

Prereqs: Docker, an SA JSON key with `roles/datastore.user` on
`sample-gcp-project-id`, a Gemini API key.

```bash
# 1. .env from template, fill in values
cp .env.example .env
$EDITOR .env                              # GOOGLE_APPLICATION_CREDENTIALS path,
                                          # GEMINI_API_KEY

# 2. Pre-flight (checks Docker memory, port collisions, .env, creds)
make doctor

# 3. Build images + initialize DuckDB
make setup

# 4. Start the stack
make up

# 5. Verify Firestore read auth (single-doc probe — no LLM cost)
make firestore-ping

# 6. First bounded ingest — eyeball a few before draining
make ingest LIMIT=5
make db-query Q='SELECT entry_id, headline, sentiment_label FROM catalysts ORDER BY id LIMIT 5'

# 7. (when happy) drain the rest
make ingest

# 8. Render PNGs + view
make render-batch
make frontend                             # tries to auto-open; on WSL it usually just prints the URL

# 9. Open the URL in your browser
#    http://localhost:8000
#    (this is the local nginx serving frontend/ + the cards.json + card_images/ from data/exports/)
```

## 6. Daily local loop (warm)

```bash
make up                                   # start (idempotent)
make ingest                               # pull new findings, extract, export
make render-batch                         # render any missing PNGs
make frontend                             # tries to auto-open browser; otherwise see URL below

# Then in a browser tab:
#   http://localhost:8000
```

`make ingest` is safe to re-run — the watermark + set-difference filter make
it a no-op when there's nothing new.

## 7. Coming back after a code change

What did you edit?

| Edit | Command |
|---|---|
| `src/*.py`, `tools/robotics-*/`, `config/config.yaml`, requirements | `make setup && make down && make up` |
| `.env` | `make down && make up` |
| `dev-utils/`, `frontend/`, `templates/`, `data/` | nothing — live |
| `tools/nginx-dev.conf` | `docker compose restart frontend-dev` |
| `Makefile` | nothing — next `make` invocation picks it up |

## 8. Prod deploy (when you're ready to flip)

Full runbook: **`DEPLOYMENT.md`**. Short version:

```bash
cp .env.prod.example .env.prod && $EDITOR .env.prod   # prod values + secrets
make deploy                                           # build + push + terraform apply
make firestore-sync                                   # one-time: local graph → CKG-Robotics
make deploy-frontend                                  # frontend → Firebase Hosting
```

`make deploy` reads `.env.prod` and does everything — generates Terraform
vars, pushes secrets to Secret Manager, zips/builds artifacts, `terraform
apply`. You never hand-edit Terraform or Cloud Run config.

What flips on automatically (fixed in the Terraform module, not `.env.prod`):

- `FIRESTORE_EXPORT_ENABLED=true`, `STORAGE_UPLOAD_ENABLED=true`, `DUCKDB_GCS_BUCKET=robotics-data`
- Cloud Scheduler fires ingest `0 6 * * *` UTC
- Pub/Sub fans out ingest → render

Prod keeps the DuckDB file in GCS (`gs://robotics-data/robotics.duckdb`);
ingest/render copy it down per run and ingest copies it back (`src/duckdb_sync.py`).
Local is unaffected — `DUCKDB_GCS_BUCKET` is unset, so the bind-mounted file
is used.

Still manual post-deploy:

- Copy `frontend/firebase-config.js.example` → `frontend/firebase-config.js`, fill from Firebase Console
- Set `FRONTEND_DATA_SOURCE=firestore`, redeploy the frontend bundle
- Verify the first scheduled run: `gcloud functions logs read robotics-ingest`

## 9. Frontend sign-in gate, share links, user admin

### Deferred sign-in gate

Anonymous visitors browse the 4-card public teaser (`preview.json`, baked
by `build_preview.py` at deploy — 4 newest is by design). The sign-in
modal trips at **60 s after first paint or the 2nd card open**, whichever
first (`GATE_DELAY_MS` / `GATE_CARD_OPENS` at the top of
`frontend/assets/auth.js`), then it's full-screen and non-dismissable.
The trip persists per browser (`localStorage rauth.gateTripped`) — return
visits gate immediately. Signed-in users are never gated. IP-based
tracking is not possible client-side; per-browser only.

After sign-in the full catalyst set + graph load live from Firestore
(`CKG-Robotics`); the teaser is only ever the 4 public cards.

### Card share links & social previews

- Share buttons emit `https://robotics.arboryx.ai/card/<card_id>`.
- Firebase Hosting rewrites `/card/**` and `/card-img/**` (before the SPA
  catch-all) to **robotics-og** — a Cloud Run service running the same
  image as robotics-render with `OG_ONLY=true` (render endpoints 404,
  no Chromium/DuckDB touched). It serves per-card OG/Twitter tags
  (headline/subtitle from Firestore, generic fallback) and streams the
  pipeline's existing PNG from the private robotics-cards bucket.
  Crawlers read the tags; humans get bounced to `/?card=<id>`, which the
  SPA normalizes to `#card-<id>` and opens (pre- or post-sign-in).
- robotics-og scales to zero; it only runs when a `/card/` URL is hit.
  It has the `allUsers` invoker; robotics-render stays IAM-locked.
- Shipping changes to it: `make deploy FORCE=1` (same image tag → build
  is skipped without FORCE) then `make deploy-frontend` for rewrites.
  Never distribute `/card/` links before both have shipped.
- Tests: `tools/robotics-render/test_og.py` — offline, needs only flask
  in a venv (`python3 test_og.py`).

### Asset caching

`assets/**` serves with `Cache-Control: public, max-age=300,
must-revalidate` (firebase.json). It was 24 h once — which let browsers
run stale JS for a day after a deploy (symptom: gate never trips, stuck
on 4 teaser cards, `deferredGate is not a function` in the console).
Keep it at 5 min; a hard refresh (Ctrl+Shift+R) clears a stuck client.

### User admin — `tools/user-admin/main.py`

Stdlib-only CLI over Identity Toolkit + Firestore REST; auth from
`gcloud auth print-access-token`, project from `--project` /
`GCP_PROJECT` / `.env.prod`.

```bash
python3 tools/user-admin/main.py count                    # total signups
python3 tools/user-admin/main.py list [--limit N]         # newest first
python3 tools/user-admin/main.py get <uid-or-email>       # auth + users/{uid} doc
python3 tools/user-admin/main.py set <uid> field=value    # patch profile doc
python3 tools/user-admin/main.py disable <uid>            # or enable
```

`set` coerces `true/false/null/ints/floats`, else string. `get` redacts
password hashes. Profile docs (`users/{uid}`) are written by the frontend
on every sign-in: uid, email, phoneNumber, displayName, photoURL,
provider, lastSeenAt, createdAt.

Auth providers enabled (verified via API): Google, Email/Password,
Phone. X/GitHub deferred. Firestore rules release matches the repo file.
`storage.rules` is intentionally undeployed — the cards bucket isn't
Firebase-linked; privacy is IAM-level (see workbench 2026-07-05).

## 10. Cleanups

| Need | Command |
|---|---|
| Reclaim disk after rebuilds (dangling images + build cache) | `make prune` |
| Stop containers, keep data | `make down` |
| Stop containers + wipe `data/` (DuckDB + exports) | `make nuke` |
| Wipe DuckDB but keep exports | `rm data/robotics.duckdb` then `make up` |
| Remove a stale legacy DB file (e.g. `data/arbor.duckdb` from before a rename) | `rm data/<file>.duckdb` — verify nothing references it first |

## 11. Troubleshooting — symptom → file

| Symptom | First place to look |
|---|---|
| `make ingest` returns 500 | `docker compose logs robotics-ingest` |
| Auth error on Firestore read | `.env` `GOOGLE_APPLICATION_CREDENTIALS` path; `make firestore-ping` |
| `400 The query requires an index` | Arboryx maintains the index we need (`category ASC, timestamp ASC, __name__ ASC`). If it's missing: `./dev-utils/firestore_indexes_bootstrap.sh` to inspect; `--apply` to create. |
| Watermark looks wrong / re-extracting old entries | `make watermark` to inspect; `tools/duckdb/init.sql` if schema mismatch |
| "Could not load cards.json — using offline preview" banner at http://localhost:8000 | `data/exports/cards.json` doesn't exist yet — run `make ingest` (it's the file `export_cards()` writes at the end of a successful run). nginx serves it via the alias in `tools/nginx-dev.conf:17`. |
| Ingest succeeded but `"export": {"error": "..."}` in the response | DuckDB has the catalysts, but cards.json wasn't written. Run `make export` to regenerate from the current DB without re-ingesting. |
| Edited `src/export.py`, want cards.json to reflect changes | `make setup && make down && make up && make export` — no LLM cost, just re-runs the JSON build. |
| Cards page empty (no banner) | check `data/exports/cards.json` exists and has cards; nginx logs |
| Card image missing | `data/exports/card_images/{entry_id}.png` exists? `make render CARD_ID=...` for one; `make render-status` for overall backlog count |
| Need to render the PNG backlog without blocking on Chromium for hours | `make render-batch ORDER=oldest LIMIT=10 SINCE_DAYS=0` — chunked from oldest, idempotent (file-exists IS the cursor; rerun picks up next chunk) |
| Stack won't start | `make doctor` (Docker memory, port collisions, creds path) |
| Edited code, change didn't take effect | image-baked path? `make setup` to rebuild |
| Container has stale env | `make down && make up` — env captured at create time |
| What changed last | top of `docs/workbench.md` |
| Prod: sign-in gate never appears, stuck on 4 cards | Stale cached `assets/auth.js` — hard refresh (Ctrl+Shift+R); cache is 5 min since 2026-07-04 (§9) |
| Prod: shared `/card/` link shows generic logo, not the card image | robotics-og deployed? (`make deploy FORCE=1`) rewrites shipped? (`make deploy-frontend`) then re-scrape via the platform's card validator |
| Who signed up / edit a user profile | `python3 tools/user-admin/main.py list` / `set <uid> field=value` (§9) |

## 12. Not in this manual

For deeper context, in order of how often you'll need them:

- **`docs/workbench.md`** — recent decisions and why; read top entry first
- **`docs/technical_spec.md`** §2.1 (ingest), §2.5 (export contract), Mermaid diagrams
- **`docs/infra_spec.md`** §3 (component choices), §5 (SA + buckets)
- **`docs/implementation_spec.md`** — ticket-level Phase 1 plan
