# `tools/` — Deployable units for the Robotics module

Each subdirectory is a self-contained tool. Every tool has:

- `Dockerfile` — container image definition
- `README.md` — local usage + deployment
- `deploy.sh` — one-shot deploy to its GCP target
- `requirements.txt` (Python tools) — pinned dependencies

See `docs/infra_spec.md` for the full architecture. Short index:

| Tool | Local role | GCP target |
|---|---|---|
| [`duckdb/`](./duckdb/) | Named volume + CLI container | Cloud Storage-backed DuckDB file |
| [`robotics-ingest/`](./robotics-ingest/) | GCS → extract → DuckDB | Cloud Functions Gen 2 |
| [`robotics-render/`](./robotics-render/) | card data → PNG (Playwright) | Cloud Run service |
| [`robotics-social/`](./robotics-social/) | (retired 2026-07-05 — social posting moved to soljet-postiz) | n/a |
| [`frontend-deploy/`](./frontend-deploy/) | n/a (local uses `frontend-dev` nginx) | Firebase Hosting |
| [`infra/`](./infra/) | Terraform — GCP resources | n/a (provisions the targets above) |

## Bootstrap

```bash
cp .env.example .env              # fill in keys (see below)
make setup                        # build images, init DuckDB
make up                           # start local dev stack
```

Required env vars (see `.env.example` at repo root for full list):

- `GCS_SA_JSON_PATH` — path to read-only SA key for `gs://sample-gcp-project-id`
- `GEMINI_API_KEY`

## Tool conventions

1. **Local = prod image.** The container you build locally is the one that deploys. Do not maintain separate dev/prod images.
2. **Entrypoints are idempotent.** Every tool is safe to invoke twice back-to-back without double-writing.
3. **Configuration > hardcoding.** Tool reads from `config/config.yaml` + env, never hardcodes project IDs or bucket names.
4. **Structured logging.** stdout in JSON lines (`{"ts": ..., "level": ..., "event": ..., ...}`) so Cloud Logging parses cleanly.

## Adding a new tool

1. `mkdir tools/my-tool && cd tools/my-tool`
2. Start from the closest existing tool as a template (ingest for Functions-style, render for Cloud Run service).
3. Add a service block to root `docker-compose.yml`.
4. Add a Terraform resource in `tools/infra/`.
5. Update this README's tool table.
