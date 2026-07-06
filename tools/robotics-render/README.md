# `tools/robotics-render` — Social card PNG renderer

HTML card template + DuckDB card row → 1200×630 PNG via Playwright/Chromium.
PNGs feed into Postiz posts and are served directly from Firebase Hosting for `og:image` previews.

## Local usage

```bash
# Render a single card
curl -X POST http://localhost:8081/render \
    -H "Content-Type: application/json" \
    -d '{"card_id":"ROB-041726-001"}'

# Render all recent cards (skips already-rendered)
curl -X POST http://localhost:8081/render-batch \
    -H "Content-Type: application/json" \
    -d '{"since_days":30}'

# Force re-render
curl -X POST http://localhost:8081/render-batch \
    -H "Content-Type: application/json" \
    -d '{"since_days":30,"force":true}'
```

PNGs land in `/data/exports/card_images/{card_id}.png` on the shared volume.

## Template

Reads `templates/card.html` (Jinja2) from the `TEMPLATES_DIR` mount. The template receives a `card` dict with: `entry_id`, `timestamp`, `headline`, `finding`, `sentiment_label`, `source_url`, `sentiment_takeaways`, `guidance_play`.

Base CSS from `templates/cards/base.css` should be inlined into `card.html` (or the template should `{% include %}` it) so Playwright doesn't need network access to the CSS file.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DUCKDB_PATH` | `/data/robotics.duckdb` | Read-only DuckDB connection |
| `CARD_IMAGES_DIR` | `/data/exports/card_images` | PNG output |
| `TEMPLATES_DIR` | `/templates` | Jinja2 template root (mount `templates/cards/`) |
| `VIEWPORT_WIDTH` / `VIEWPORT_HEIGHT` | `1200` / `630` | Twitter/LinkedIn card ratio |
| `PORT` | `8081` | HTTP port |

## GCP target — Cloud Run service

```bash
GCP_PROJECT_ID=robotics-prod ./deploy.sh
```

- `--concurrency=1` — Chromium is memory-heavy; one request per instance.
- `--max-instances=5` — parallel batch rendering without runaway cost.
- `--min-instances=0` — scale to zero when idle.
- `--memory=2Gi --cpu=2` — comfortable headroom for Playwright.
- `--no-allow-unauthenticated` — invoked only by robotics-ingest / Cloud Scheduler via OIDC.

## Cold start

First request after scale-to-zero takes ~8-15s (Chromium launch). Subsequent requests hit the warmed instance. If UX latency is an issue, pin `min-instances=1` (costs ~$20/month idle).

## Why Cloud Run (not Functions)

- Chromium image is ~1.5GB — too heavy for Functions' deployment workflow.
- Playwright base image (`mcr.microsoft.com/playwright/python`) is container-first.
- Cloud Run's per-request billing + scale-to-zero is identical to Functions Gen 2 economically.

## Implementation status

- [x] `/render` + `/render-batch` + `/healthz` endpoints
- [x] Playwright Chromium launch, screenshot at 1200×630
- [x] Idempotent batch (skip existing PNGs)
- [ ] GCS upload path (Phase 1 ticket 1.16 polish — PNGs currently land on volume only)
- [ ] Template receives share texts + entity chips (wired when export contract lands)
