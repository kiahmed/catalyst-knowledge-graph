# `tools/duckdb` — Local analytical store

Holds the Robotics-module DuckDB file on a persistent Docker volume. Other tools mount the same volume to read/write.

## Local usage

The `duckdb` service is started by root `docker-compose.yml`. It initializes the schema on first boot from `init.sql` (idempotent — safe to rerun).

```bash
# Start (from repo root)
docker compose up -d duckdb

# Interactive SQL shell
docker compose exec duckdb duckdb /data/robotics.duckdb

# One-off query
docker compose exec duckdb duckdb /data/robotics.duckdb -c "SELECT COUNT(*) FROM catalysts;"

# Backup the file
docker compose cp duckdb:/data/robotics.duckdb ./robotics.backup.duckdb

# Nuke (stop + delete volume)
docker compose down -v
```

## Volume

Named volume `robotics-data` mounted at `/data`. Paths:

- `/data/robotics.duckdb` — the database file
- `/data/exports/cards.json` — JSON export written by `robotics-ingest`
- `/data/exports/card_images/*.png` — PNGs written by `robotics-render`

Other tools mount this same volume read-only (`robotics-social`) or read-write (`robotics-ingest`, `robotics-render`).

## Schema source of truth

`init.sql` mirrors `docs/technical_spec.md` §2.4. When `src/db.py` ships:

- `src/db.py` owns the runtime schema creation logic.
- `init.sql` stays as the first-run container bootstrap.
- Both must stay in sync — unit test `tests/test_schema_parity.py` (Phase 1 ticket) reads both and diffs the statements.

## GCP target

DuckDB is not a managed service — there is no GCP container. In prod, the DuckDB file lives in `gs://robotics-data/robotics.duckdb` and every tool:

1. Downloads the object to local scratch on cold start.
2. Reads/writes locally.
3. Uploads back with `if_generation_match=<current generation>` for optimistic concurrency.

Single-writer model enforced by `--max-instances=1` on the ingest Cloud Function. Readers (render, social) never write.

## When to replace DuckDB

Trigger thresholds (document in `docs/workbench.md` if hit):

- Row count > 1M → evaluate BigQuery migration
- Multi-writer required → introduce Cloud Run write gateway or move to BigQuery
- File size > 500MB → switch to DuckDB `httpfs` extension (no download-every-run)

None of these are Phase 1 concerns.
