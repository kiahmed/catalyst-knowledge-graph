# `tools/robotics-social` — Postiz poster

Selects top-N cards from DuckDB → uploads PNGs to Postiz → schedules cross-platform posts → records post IDs in `social_posts` for later analytics pull.

## Local usage

```bash
# Start the HTTP surface (default mode in docker-compose)
docker compose up robotics-social

# Trigger a batch
curl -X POST http://localhost:8082/run \
    -H "Content-Type: application/json" \
    -d '{"dry_run":true}'

# Or one-shot CLI mode inside the container
docker compose run --rm robotics-social python main.py --once --dry-run
```

Makefile shortcut: `make social`.

## Selection criteria

From `catalysts` × `relationships`, where:

- `max(confidence) >= MIN_CONFIDENCE` (default `0.75`)
- `entry_id` not already in `social_posts`

Ranked by (timestamp DESC, max_confidence DESC). Caps at `DAILY_POST_CAP` (default 3). No age filter — unposted high-confidence catalysts from any date are eligible; re-running the command walks backward through the DB until it exhausts candidates.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `POSTIZ_BASE_URL` | `http://localhost:4200` | Postiz API root (local sibling `soljet-postiz`) |
| `POSTIZ_API_KEY` | required in prod | Auth |
| `POSTIZ_CHANNELS` | `twitter,linkedin` | Comma-separated channel list |
| `DAILY_POST_CAP` | `3` | Max posts per run |
| `MIN_CONFIDENCE` | `0.75` | Filter threshold |
| `DUCKDB_PATH` | `/data/robotics.duckdb` | Read-write DuckDB connection |
| `CARD_IMAGES_DIR` | `/data/exports/card_images` | PNG source |

## GCP target — Cloud Run Job

```bash
GCP_PROJECT_ID=robotics-prod ./deploy.sh
```

- Cloud Run **Job** (not service) — correct shape for one-shot batches.
- Triggered by Cloud Scheduler `0 7 * * *` (daily 07:00 UTC, one hour after ingest).
- Entry point: `python main.py --once`.
- Secrets: `POSTIZ_API_KEY`, `POSTIZ_BASE_URL` pulled from Secret Manager.
- No OIDC ingress — executed directly via `gcloud run jobs execute`.

## Analytics follow-up (Phase 1 ticket 1.18)

24h after post, pull `/analytics/{postiz_id}` → update `social_posts.impressions` / `engagements` / `clicks`. Scheduled as a second Cloud Run Job (`robotics-social-analytics`) triggered daily.

## Implementation status

- [x] Candidate selection SQL
- [x] Postiz upload + schedule flow
- [x] `social_posts` recording
- [x] Dual-mode entry (`--once` for prod, `--serve` for local)
- [ ] Share text sourced from `cards.json` (stub currently uses headline)
- [ ] Analytics follow-up job (Phase 1 ticket 1.18)
- [ ] Channel-specific text variants (Twitter vs LinkedIn length limits)
