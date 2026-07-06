# `tools/robotics-ingest` — Ingestion function

HTTP-triggered ingestion: Arboryx Firestore findings → extraction → DuckDB.
Same code runs locally in Docker and in Cloud Functions Gen 2 unchanged.

> **Source-of-truth:** Arboryx writes each finding to `findings/{entry_id}` in
> Firestore (project `sample-gcp-project-id`, default DB). The legacy GCS
> master log was retired on 2026-05-10.

## Local usage

Started by root `docker-compose.yml` as service `robotics-ingest` on port 8080.

```bash
# Trigger an ingest run locally
curl -X POST http://localhost:8080 \
    -H "Content-Type: application/json" \
    -d '{"sector":"Robotics","dry_run":true}'

# Full ingest (writes to mounted DuckDB volume)
curl -X POST http://localhost:8080 \
    -H "Content-Type: application/json" \
    -d '{"sector":"Robotics"}'

# Bounded run — process at most N oldest-after-watermark entries
curl -X POST http://localhost:8080 \
    -H "Content-Type: application/json" \
    -d '{"sector":"Robotics","limit":5}'
```

Or use the Makefile shortcut: `make ingest` (or `make ingest LIMIT=5`).

## Request contract

```json
POST /
{
    "sector": "Robotics",     // optional — defaults to env $SECTOR then "Robotics"
    "dry_run": false,         // optional — skips writes + export, returns counts
    "limit": null             // optional — if >0, process at most N oldest-after-watermark entries
}
```

Response 200:
```json
{
    "ok": true,
    "sector": "Robotics",
    "entries_read": 182,
    "entries_new": 3,
    "entries_written": 3,
    "last_processed_entry_id": "ROB-041726-001",
    "duration_s": 12.4
}
```

Error 500 → `{"ok": false, "error": "..."}`

## Environment variables

| Var | Local default | Purpose |
|---|---|---|
| `GCP_PROJECT` | `sample-gcp-project-id` | Arboryx Firestore project |
| `FIRESTORE_DATABASE` | `(default)` | Firestore database id (Arboryx uses default) |
| `FIRESTORE_COLLECTION` | `findings` | Source collection (one doc per finding) |
| `DUCKDB_PATH` | `/data/robotics.duckdb` | Local DuckDB file (mounted from `duckdb` container) |
| `DUCKDB_GCS_BUCKET` | (unset locally) | Prod: where the DuckDB file lives (Robotics state bucket — distinct from Arboryx) |
| `SECTOR` | `Robotics` | Default sector filter |
| `GEMINI_API_KEY` | required | LLM auth |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/secrets/gcs-sa.json` | GCP auth locally; auto-attached by Cloud Functions in prod. SA needs `roles/datastore.user` |
| `LOG_LEVEL` | `INFO` | Python logging level |

## GCP target — Cloud Functions Gen 2

Deploy:
```bash
GCP_PROJECT_ID=robotics-prod ./deploy.sh
```

Config choices (`deploy.sh`):

- `--max-instances=1 --concurrency=1` → single-writer enforcement for DuckDB.
- `--timeout=540s` → 9 min. Fine for incremental ingest.
- `--memory=2Gi` → headroom for DuckDB + LLM client. Tune down after measuring.
- `--no-allow-unauthenticated` → Cloud Scheduler invokes with OIDC token.
- Secrets attached via `--set-secrets`, not env — never logged.

## Scheduling

Cloud Scheduler job (provisioned by `tools/infra/cloud_scheduler.tf`):

- Schedule: `0 6 * * *` (daily 06:00 UTC)
- Target: HTTPS + OIDC token with `robotics-scheduler-sa`
- Retry: 3 attempts, exponential backoff
- On success, publishes to `robotics-ingest-done` Pub/Sub topic → fans out to render + social

## Implementation status

- [x] HTTP boundary + structured logging
- [x] Firestore read + sector filter + watermark read
- [x] Request schema + response shape
- [ ] Extraction pipeline (ticket 1.6 — lands in `src/extract.py`, imported here)
- [ ] Entity resolver (ticket 1.7 — `src/resolve.py`)
- [ ] DuckDB write + watermark advance (ticket 1.11)
- [ ] Idempotency test suite (ticket 1.12)
