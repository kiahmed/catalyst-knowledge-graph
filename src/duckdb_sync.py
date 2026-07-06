"""Pull/push the DuckDB file between local disk and a GCS bucket.

Prod runs ingest + render as stateless containers, so the DuckDB file lives
in a GCS bucket at rest and is copied to local disk for the duration of a
run. Local dev sets no bucket, so both functions are no-ops and the
bind-mounted file under data/ is used directly.

Gating: the `DUCKDB_GCS_BUCKET` env var.
    unset / empty  → local mode  → pull()/push() are no-ops
    set            → prod mode   → object is gs://{bucket}/{basename(duckdb_path)}

Single-writer contract: only robotics-ingest calls push(); robotics-render
calls pull() only (it opens DuckDB read-only). Ingest runs max-instances=1,
so there is never a concurrent writer and a plain object overwrite is safe.

push() must be called only AFTER the DuckDB connection is closed (or an
explicit CHECKPOINT has run) — DuckDB folds the WAL into the main .duckdb
file on checkpoint, so the single file is self-consistent with no .wal.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("robotics.duckdb_sync")


def _bucket_name() -> str:
    return os.environ.get("DUCKDB_GCS_BUCKET", "").strip()


def enabled() -> bool:
    """True when running in prod mode (a GCS bucket is configured)."""
    return bool(_bucket_name())


def _object_name(duckdb_path: str) -> str:
    # gs://{bucket}/robotics.duckdb — basename keeps the object in lockstep
    # with DUCKDB_PATH so local and prod never drift.
    return os.path.basename(duckdb_path)


def pull(duckdb_path: str) -> dict:
    """Download the DuckDB file from GCS to `duckdb_path`.

    No-op in local mode. When the remote object does not exist yet (the very
    first prod run), returns {"pulled": False, "reason": "no_remote_object"}
    so the caller proceeds to create a fresh schema on local disk.

    Raises on real GCS errors (auth, network) — the caller should treat that
    as fatal, since proceeding would silently start from an empty DB.
    """
    bucket_name = _bucket_name()
    if not bucket_name:
        return {"pulled": False, "reason": "local_mode"}

    from google.cloud import storage

    obj = _object_name(duckdb_path)
    blob = storage.Client().bucket(bucket_name).blob(obj)
    if not blob.exists():
        log.info("duckdb_pull skipped — no remote object gs://%s/%s", bucket_name, obj)
        return {"pulled": False, "reason": "no_remote_object"}

    os.makedirs(os.path.dirname(duckdb_path) or ".", exist_ok=True)
    blob.download_to_filename(duckdb_path)
    size = os.path.getsize(duckdb_path)
    log.info("duckdb_pull ok gs://%s/%s -> %s (%d bytes)", bucket_name, obj, duckdb_path, size)
    return {"pulled": True, "object": f"gs://{bucket_name}/{obj}", "bytes": size}


def push(duckdb_path: str) -> dict:
    """Upload the DuckDB file at `duckdb_path` back to GCS.

    No-op in local mode. Call only after the DuckDB connection is closed.
    """
    bucket_name = _bucket_name()
    if not bucket_name:
        return {"pushed": False, "reason": "local_mode"}
    if not os.path.exists(duckdb_path):
        log.warning("duckdb_push skipped — local file missing: %s", duckdb_path)
        return {"pushed": False, "reason": "no_local_file"}

    from google.cloud import storage

    obj = _object_name(duckdb_path)
    blob = storage.Client().bucket(bucket_name).blob(obj)
    blob.upload_from_filename(duckdb_path)
    size = os.path.getsize(duckdb_path)
    log.info("duckdb_push ok %s -> gs://%s/%s (%d bytes)", duckdb_path, bucket_name, obj, size)
    return {"pushed": True, "object": f"gs://{bucket_name}/{obj}", "bytes": size}
