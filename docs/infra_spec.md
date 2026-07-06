# Infrastructure Spec — Catalyst knowledge-graph (Robotics module)

*Local Docker dev stack + GCP target architecture. Decisions locked 2026-04-17. Consumers: `technical_spec.md` (data flow), `implementation_spec.md` (ticket ordering), every tool under `tools/`.*

---

## 1. Principles

1. **Local-first.** Everything builds and runs on a developer laptop with `docker compose up`. No GCP credentials required beyond the read-only service account that reads `findings/*` from Arboryx's Firestore.
2. **Dev/prod parity.** Every tool ships as a container. The container that runs on your laptop is the same image that runs in GCP. No "works on my machine" bugs.
3. **One tool = one deployable.** Each subdirectory under `tools/` is a self-contained unit with Dockerfile + entry point + deploy script + README. Nothing crosses tool boundaries except via the JSON export contract or DuckDB.
4. **GCP-deploy-ready from day one, deployed when ready.** All tools are designed to drop into their GCP target (Cloud Functions Gen 2, Cloud Run, Cloud Run Job, Firebase Hosting) with zero code changes. Actual deployment waits for Phase 1 gate.
5. **Reusable & scalable.** Tool structure, container conventions, and Terraform modules are patterns that can be copied when Arboryx adds another sector module alongside Robotics.

---

## 2. Local dev stack (Docker Compose)

```
┌──────────────────────────────────────────────────────────────────────┐
│  docker compose up                                                   │
│                                                                      │
│   ┌──────────────┐    ┌──────────────────────┐                       │
│   │  duckdb      │◀───│  robotics-ingest         │  (functions-framework)│
│   │  (vol mount) │    │  Firestore → DuckDB  │                       │
│   └──────▲───────┘    └─────────┬────────────┘                       │
│          │                      │                                    │
│          │                      ▼                                    │
│          │              (reads Arboryx findings/                     │
│          │               from Firestore                              │
│          │               project sample-gcp-project-id)              │
│          │                                                           │
│          │              ┌────────────────────┐                       │
│          ├──────────────│  robotics-render      │  (Playwright +        │
│          │              │  cards.json → PNG  │   Chromium)           │
│          │              └─────────┬──────────┘                       │
│          │                        │                                  │
│          │              ┌────────────────────┐                       │
│          ├──────────────│  robotics-social      │  (Postiz API client)  │
│          │              │  PNG → Postiz      │                       │
│          │              └────────────────────┘                       │
│          │                                                           │
│          │              ┌────────────────────┐                       │
│          └──────────────│  frontend-dev      │  (nginx)              │
│                         │  serves frontend/  │                       │
│                         └────────────────────┘                       │
└──────────────────────────────────────────────────────────────────────┘

Volumes:
  robotics-data         → /data/robotics.duckdb + exports/{cards.json, card_images/}
Secrets (via .env):
  GOOGLE_APPLICATION_CREDENTIALS → SA key with roles/datastore.user on the Arboryx project
  GOOGLE_GENAI_API_KEY, POSTIZ_API_KEY, POSTIZ_BASE_URL
```

### 2.1 Container sketch

| Service | Image source | Port(s) | Purpose |
|---|---|---|---|
| `duckdb` | `datacatering/duckdb:latest` (or custom thin image — see `tools/duckdb/Dockerfile`) | — | Hosts the DuckDB file on a named volume. Not a server — DuckDB is embedded. This service exists to own the volume + provide a CLI container for ad-hoc queries. |
| `robotics-ingest` | built from `tools/robotics-ingest/Dockerfile` | 8080 | HTTP endpoint (`functions-framework`). Ingests Arboryx → DuckDB. Triggered by `curl http://localhost:8080/run` locally; Cloud Scheduler → HTTPS in prod. |
| `robotics-render` | built from `tools/robotics-render/Dockerfile` | 8081 | HTTP endpoint. Reads recent cards from DuckDB, renders PNGs into the shared volume. |
| `robotics-social` | built from `tools/robotics-social/Dockerfile` | 8082 | HTTP endpoint. Reads top-N cards, POSTs to Postiz API. |
| `frontend-dev` | `nginx:alpine` | 8000 | Serves `./frontend/` + the generated `cards.json` so you can click around locally. |

### 2.2 Local developer flow

```bash
# One-time
cp .env.example .env                # fill in SA path + API keys
make setup                          # pulls images, initializes DuckDB schema

# Daily
make up                             # start all services
make ingest                         # trigger ingestion (curl robotics-ingest)
make render                         # generate PNG previews
make frontend                       # open http://localhost:8000
make db                             # drop into DuckDB CLI

# Teardown
make down                           # stop services, keep data
make nuke                           # stop services, wipe volumes (full reset)
```

---

## 3. GCP target architecture

*This is the deployment topology. Not deployed during Phase 1; designed now so tools land on it without rework when the gate opens.*

```
                      Cloud Scheduler (cron: daily 06:00 UTC)
                              │
                              │ OIDC-authenticated HTTPS
                              ▼
                     ┌─────────────────────────┐
                     │ robotics-ingest            │   Cloud Functions Gen 2
                     │ (Python 3.12 runtime)   │   Memory 2GB, timeout 540s
                     └──────────┬──────────────┘
                                │
          ┌─────────── read ────┼──── write ──────────┐
          │                     ▼                     │
 ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
 │ gs://marketres…  │  │ gs://robotics-data  │  │ Secret Manager    │
 │   (Arboryx —   │  │ robotics.duckdb     │  │  GEMINI_API_KEY   │
 │    read-only)    │  │ exports/         │  │  POSTIZ_API_KEY   │
 └──────────────────┘  └────────┬─────────┘  └──────────────────┘
                                │
                                │ (both invoked on-demand
                                │  after ingest succeeds,
                                │  via Pub/Sub fan-out)
                                ▼
               ┌──────────────────────────────────┐
               │ robotics-render    │  robotics-social  │
               │ Cloud Run       │  Cloud Run Job │
               │ (Chromium +     │  (Postiz client │
               │  Playwright)    │   + analytics)  │
               └──────┬──────────┘─────┬──────────┘
                      │                │
                      ▼                ▼
              gs://robotics-data      Postiz API
               card_images/
                      │
                      │ (gsutil rsync on successful run)
                      ▼
           ┌──────────────────────┐
           │ Firebase Hosting     │  ← public URL (robotics.arboryx.ai)
           │  frontend/index.html │
           │  cards.json          │
           │  card_images/*.png   │
           └──────────────────────┘
```

### 3.1 Service mapping

| Tool | GCP service | Rationale |
|---|---|---|
| `robotics-ingest` | **Cloud Functions Gen 2** | HTTP trigger works for Cloud Scheduler. `functions-framework` lets the same entry point run locally AND on GCP — literally zero code difference. Gen 2 = Cloud Run under the hood → 60 min timeout, 16GB memory ceiling. Scales to zero. |
| `robotics-render` | **Cloud Run (service)** | Chromium + Playwright adds ~280MB to the image. Cloud Run handles custom containers better than Functions. Set min_instances=0, max_instances=5, concurrency=1 (each render is CPU-bound on headless Chromium). |
| `robotics-social` | **Cloud Run Job** (not service) | Jobs are explicitly one-shot batch workloads — right shape for "post N cards then exit." Triggered by Cloud Scheduler after ingest completes. |
| `frontend-deploy` | **Firebase Hosting** | Free CDN (edge locations globally), atomic deploys, instant rollback (`firebase hosting:rollback`), custom domain in 2 clicks. Alternative: Cloud Storage + Cloud CDN + Load Balancer — more config, more cost, same result. Defer that until Firebase Hosting proves insufficient (custom cache headers, multi-region failover). |
| `duckdb` | **GCS-FUSE-mounted DuckDB file** | `gs://robotics-data` mounted at `/data` inside ingest/render containers via Cloud Run Volume Mounts. DuckDB opens `/data/robotics.duckdb` directly — no download/upload step. Single-writer model (ingest function has `--max-instances=1`); render opens `read_only=True`. See §3.3 for the choice rationale and the required ingest CHECKPOINT step. When rows grow past ~1M or multi-writer becomes a need, re-evaluate BigQuery. |
| Secrets | **Secret Manager** | Gemini API key, Postiz API key. Accessed via service account at runtime. |
| Container images | **Artifact Registry** | `us-central1-docker.pkg.dev/PROJECT_ID/robotics/{tool-name}:{git-sha}`. |
| Scheduling | **Cloud Scheduler** | Cron trigger → HTTPS to ingest function. On success, Pub/Sub message fans out to render + social. |

### 3.2 What we deliberately did NOT pick, and why

| Alternative | Why not (now) |
|---|---|
| **BigQuery** as analytical store | Analytical workload is <10k rows of Robotics catalysts. DuckDB is faster, simpler, and free. BigQuery becomes right when rows > 1M, multi-writer, or BI-tool integration matters. Revisit in Phase 3. |
| **Cloud SQL (Postgres)** | Not an analytical store — wrong shape. Inferior to DuckDB for our queries. |
| **Firestore** | NoSQL is wrong for relational joins (entity ↔ catalyst ↔ relationship). Would force denormalization. |
| **App Engine** | Legacy PaaS. Cloud Run is its modern successor; Cloud Run + Functions Gen 2 is the canonical stack. |
| **GKE (Kubernetes)** | Over-engineered for 3-4 services with scale-to-zero needs. Cloud Run gets us serverless without the YAML tax. |
| **Cloud Composer (Airflow)** | Heavyweight scheduler for what Cloud Scheduler can do in two lines of Terraform. Add only if DAG complexity grows. |
| **Dataflow / Pub/Sub for ingest streaming** | Arboryx writes ~one batch of findings to Firestore per day. Cursor-based pulls are correct; streaming triggers are overkill. |
| **Next.js / SvelteKit frontend** | `frontend/index.html` is a ~400-line static bundle. Adding a framework adds build pipeline, hydration bugs, deploy complexity, and slower load. Revisit only if interactivity requirements grow beyond what vanilla JS + fetch can handle. |
| **Vercel / Netlify** | Firebase Hosting serves the same purpose inside GCP, keeping IAM / logging / billing unified. |

### 3.3 DuckDB on GCS — FUSE mount, not download/upload

We considered two ways to run DuckDB against `gs://robotics-data/robotics.duckdb` in prod:

| Option | What | Why we picked / didn't |
|---|---|---|
| **A. Download → modify → upload** | Pull the file at function cold-start, write locally, upload back with `if_generation_match`. | Reliable, well-understood, but costs 2-5s of cold-start latency per run. Originally documented here. |
| **B. GCS FUSE volume mount** ✓ | `gs://robotics-data` mounted at `/data`. DuckDB reads/writes the file in place. | Picked. Sub-second cold start. Cost difference is pennies/day. Single-writer + low-write-volume make GCS FUSE's "read-mostly" recommendation a non-issue for this workload. |

**Why FUSE works for our specific shape:**

- **Single writer**: `--max-instances=1` on the ingest Cloud Function means one DuckDB writer at a time. `robotics-render` and `robotics-social` open the same file with `read_only=True` (see `tools/robotics-render/main.py:52`).
- **Low write volume**: Arboryx's morning collection produces ~10-20 new findings/day → ~20 short transactions per ingest run. Total fsync overhead added by FUSE: ~1-4s/day.
- **Sequenced reads**: Render fires off the `robotics-ingest-done` Pub/Sub event, after writes complete. No concurrent reader-writer access.
- **No cross-instance lock coordination needed**: Cloud Storage FUSE supports `flock` within a single mount; not across mounts. With max-instances=1, there's only ever one mount writing at a time.

**Two requirements for safe FUSE-backed writes:**

1. **Mount both the main file and its WAL**: DuckDB writes `robotics.duckdb` and `robotics.duckdb.wal` side-by-side. Mounting the bucket root at `/data` ensures both files live in the same mounted directory and stay consistent.
2. **Explicit checkpoint before container exit**: Cloud Functions / Cloud Run can SIGKILL on timeout. A pending WAL on a remote filesystem is harder to reason about than on local SSD. `run_ingest()` must end with:
   ```python
   con.execute("CHECKPOINT")
   ```
   This folds the WAL into the main file so the on-disk state is self-contained even if the container is forcibly stopped between runs.

**When to revisit:**
- DuckDB file > 500MB → first-query latency for paged reads becomes noticeable; consider a hybrid (FUSE for reads, download for writes) or move to BigQuery.
- Multi-writer becomes a need → FUSE no longer enforces single-writer; switch to BigQuery or Cloud SQL.
- Per-transaction commit latency exceeds 1s under load → fsync-on-FUSE has become the bottleneck; switch to download/upload (Option A) or consider a sidecar-mediated write path.

---

## 4. `tools/` directory layout

```
tools/
├── README.md                           # Index: what each tool is, how to run it
│
├── duckdb/                             # Local DuckDB volume owner
│   ├── Dockerfile
│   ├── init.sql                        # Schema bootstrap (sourced from src/db.py schema)
│   └── README.md
│
├── robotics-ingest/                       # Firestore → extract → resolve → DuckDB
│   ├── Dockerfile                      # python:3.12-slim + functions-framework
│   ├── main.py                         # @functions_framework.http def run_ingest(req): ...
│   ├── requirements.txt
│   ├── .gcloudignore
│   ├── deploy.sh                       # gcloud functions deploy robotics-ingest ...
│   └── README.md
│
├── robotics-render/                       # card data → PNG (Playwright)
│   ├── Dockerfile                      # mcr.microsoft.com/playwright/python
│   ├── main.py                         # Flask app, /render endpoint
│   ├── requirements.txt
│   ├── deploy.sh                       # gcloud run deploy robotics-render ...
│   └── README.md
│
├── robotics-social/                       # DuckDB top-N → Postiz
│   ├── Dockerfile
│   ├── main.py                         # One-shot CLI: python main.py
│   ├── requirements.txt
│   ├── deploy.sh                       # gcloud run jobs deploy robotics-social ...
│   └── README.md
│
├── frontend-deploy/                    # Firebase Hosting deploy
│   ├── deploy.sh                       # runs firebase from repo root, --only hosting
│   ├── storage.rules                   # Firebase Storage Security Rules
│   └── README.md
│   # firebase.json + .firebaserc live at the REPO ROOT — Firebase needs the
│   # config and public dir (frontend/) in one tree.
│
└── infra/                              # Terraform — GCP resources
    ├── main.tf                         # Provider, backend (GCS state bucket)
    ├── variables.tf
    ├── outputs.tf
    ├── bucket.tf                       # robotics-data bucket + lifecycle rules
    ├── artifact_registry.tf
    ├── secrets.tf                      # Secret Manager resources (values set via gcloud)
    ├── service_accounts.tf             # Per-tool SAs with least-privilege bindings
    ├── cloud_functions.tf              # robotics-ingest
    ├── cloud_run.tf                    # robotics-render (service) + robotics-social (job)
    ├── cloud_scheduler.tf              # Daily cron
    ├── pubsub.tf                       # Fan-out topic + subscriptions
    └── README.md
```

### 4.1 Per-tool conventions

Every `tools/{name}/` directory MUST contain:
- `Dockerfile` — builds the deployable image. Multi-stage if compile-heavy.
- `README.md` — "How to run locally" + "How to deploy" + "What env vars it needs."
- `deploy.sh` — one-command deploy to the target GCP service (used locally OR from CI).
- `requirements.txt` (Python tools) — pinned deps. `pip-compile` the source file.

Every `tools/{name}/` directory MAY contain:
- `main.py` — entry point (convention).
- `.gcloudignore` — exclude `__pycache__`, tests, etc. from upload.
- `tests/` — unit tests for the tool.

---

## 5. Configuration & secrets

### 5.1 Layered config

| Layer | What lives there | Example |
|---|---|---|
| `.env` (root, gitignored) | Local secrets + paths | `GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa.json`, `GCP_PROJECT=sample-gcp-project-id`, `GEMINI_API_KEY=...` |
| `.env.example` (committed) | Schema reference | Documents every var name with comments, no values |
| `config/config.yaml` | Non-secret runtime config | Model name, DuckDB path, Postiz cap, schema_version |
| Secret Manager (prod) | Secrets, fetched at runtime | `GEMINI_API_KEY`, `POSTIZ_API_KEY` |

### 5.2 Service accounts (prod — least privilege)

| SA | Role bindings |
|---|---|
| `robotics-ingest-sa` | `roles/datastore.user` on `sample-gcp-project-id` (Firestore read of `findings/*` AND write of `CKG-Robotics/*`), `roles/storage.objectUser` on `robotics-data`, `roles/secretmanager.secretAccessor` on `GEMINI_API_KEY`, `roles/pubsub.publisher` on `robotics-ingest-done` |
| `robotics-render-sa` | `roles/storage.objectUser` on `robotics-data`, `roles/storage.objectCreator` on `robotics-cards` (write-only PNG uploads — public-read is at the bucket level) |
| `robotics-social-sa` | `roles/storage.objectViewer` on `robotics-data`, `roles/secretmanager.secretAccessor` on `POSTIZ_API_KEY` |
| `robotics-scheduler-sa` | `roles/cloudfunctions.invoker` on `robotics-ingest`, `roles/run.invoker` on `robotics-render`, `roles/run.jobs.invoker` on `robotics-social` |

**Trust boundary on `robotics-ingest-sa`'s `datastore.user`:** Firestore IAM has no collection-scoped roles; granting write on the `(default)` database also grants write on Arboryx's `findings/*`. Code paths in `src/ingest.py` only read `findings/*` and only write to `CKG-Robotics/*`. Cloud Audit Logs surface any anomalous write. Splitting databases would remove this concern but adds operational overhead per sector — revisit if multi-tenant isolation becomes a hard requirement.

Frontend needs no SA — Firebase Hosting serves static assets anonymously, and the cards bucket is public-read at the bucket level.

### 5.2a Buckets

| Bucket | Visibility | Contents | Lifecycle |
|---|---|---|---|
| `robotics-data` | Private (uniform IAM) | DuckDB file (single-writer), JSON exports, raw render cache | Versioning on; archived versions deleted at 30d; raw `exports/card_images/*` deleted at 90d |
| `robotics-cards` | Public-read (uniform IAM) | Card PNGs the Firebase frontend fetches at `https://storage.googleapis.com/robotics-cards/cards/{entry_id}.png` | All objects deleted at 90d (re-rendered on demand) |

### 5.3 Secrets flow

Local → `.env` → Docker Compose `env_file`.
Prod → Secret Manager → Cloud Function/Run attaches secret as env var via `--set-secrets KEY=secret:latest`.
CI → Workload Identity Federation (no JSON keys in GitHub). Future work.

---

## 6. CI / CD (Phase 1 end — not day one)

Not built in Phase 1 ticket 1.1. Documented here so the tool structure above is ready for it.

**Target:** GitHub Actions pipeline. On push to `main`:
1. Per tool, build container → push to Artifact Registry with `git-sha` tag.
2. Deploy each tool via `tools/{name}/deploy.sh` using the new image tag.
3. Deploy frontend via `tools/frontend-deploy/deploy.sh`.
4. Smoke test: curl the ingest endpoint with `?dry_run=true` and assert 200.

Authentication: Workload Identity Federation (GitHub OIDC → GCP SA). No long-lived keys.

---

## 7. Scalability & reuse story

This layout is designed to be copied, not just built once.

**When Arboryx adds a second sector** (Phase 3 "AI Stack"):
- `tools/robotics-ingest/` is already sector-parameterized (env var `SECTOR`). Deploy a second Cloud Function instance with `SECTOR=ai_stack`.
- Same DuckDB, same frontend — only the sector filter changes.

**When Arboryx activates a second module** (e.g., a Biotech module alongside Robotics):
- Fork the repo, change `config/config.yaml`, rebuild.
- `tools/` Terraform module is reusable as-is with different project/bucket names.
- Frontend branding swap is a single `frontend/assets/app.css` edit.

**When scale forces a rearchitecture:**
- DuckDB row count > 1M → migrate to BigQuery. Keep `tools/robotics-ingest/` shape; swap the write target.
- Multi-writer required → introduce Cloud Run service as a write gateway in front of DuckDB, keep ingest as a message producer.
- Sub-minute freshness required → Pub/Sub event-driven ingest instead of Cloud Scheduler cron.

Each of these is a single-tool swap, not a platform rewrite.

---

## 8. Phase 1 deliverables (what ships during this spec's validity)

| Deliverable | Where | Who builds |
|---|---|---|
| Local dev stack (Docker Compose up) | Root `docker-compose.yml`, `tools/duckdb/`, `Makefile` | Ticket 1.1a (new — infra setup) |
| `robotics-ingest` tool (local-runnable, deploy script ready) | `tools/robotics-ingest/` | Ticket 1.10 (ingest) as before, repackaged into the tool |
| `robotics-render` tool | `tools/robotics-render/` | Ticket 1.16 (PNG renderer) |
| `robotics-social` tool | `tools/robotics-social/` | Tickets 1.17-1.18 (Postiz) |
| `frontend-deploy` tool | `tools/frontend-deploy/` | Ticket 1.23 (deploy) |
| `infra/` Terraform (documented, not `terraform apply`-ed) | `tools/infra/` | Phase 1 gate → Phase 2 week 1 |

---

## 9. Risks specific to this infra

| Risk | Mitigation |
|---|---|
| Playwright image is large (~1.5GB) | Cloud Run cold starts still acceptable (~8-15s). Multi-stage Dockerfile trims dev deps. If cold start becomes UX issue, set min_instances=1 on render service. |
| DuckDB file grows — first-query latency on FUSE-mounted file | DuckDB pages in blocks on demand from GCS. At <100MB, first query is ~1-2s; subsequent queries hit the FUSE cache. If file crosses ~500MB or first-query latency becomes user-visible (e.g. on render cold-start), switch to a hybrid: FUSE for reads, download/upload for writes — or move analytics to BigQuery. See §3.3 for the FUSE choice rationale. |
| FUSE-backed WAL not flushed on container SIGKILL | Pending WAL on a remote filesystem is harder to reason about than on local SSD. Mitigated by an explicit `con.execute("CHECKPOINT")` at the end of `run_ingest()` — folds the WAL into the main file before the container exits. Required, not optional. See §3.3. |
| Cloud Function 60-min timeout for first-time bulk ingest | First-time ingest runs locally against local DuckDB (via `make ingest`, optionally `LIMIT=N` to stage), then the file is uploaded. Production ingest is always incremental (~5 entries/day) → seconds. |
| Firebase Hosting single-region origin | Firebase CDN is already multi-region. Origin is fine. |
| GitHub Actions lock-in for CI | Pipeline is `bash` inside `deploy.sh` — portable to any CI runner. GH Actions file is a thin wrapper. |
| Cost drift (unused resources after Phase 1) | Every Cloud Run / Function scales to zero. Cloud Storage lifecycle rule deletes `card_images/*.png` older than 90 days. Estimated idle cost: <$2/month. |
