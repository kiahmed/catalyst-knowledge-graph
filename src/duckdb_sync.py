"""Pull/push the DuckDB file between local disk and a GCS bucket.

Prod runs ingest + render as stateless containers, so the DuckDB file lives
in a GCS bucket at rest and is copied to local disk for the duration of a
run. Local dev sets no bucket, so both functions are no-ops and the
bind-mounted file under data/ is used directly.

Gating: the `DUCKDB_GCS_BUCKET` env var.
    unset / empty  → local mode  → pull()/push() are no-ops
    set            → prod mode   → object is gs://{bucket}/{basename(duckdb_path)}

Writers: robotics-ingest (pull → run → push) and the operator tool
dev-utils/direction_audit.py. Every push is conditional on the generation
this process pulled (if_generation_match), so whichever writer finishes
second fails loudly ("generation_conflict") instead of silently overwriting
the other's changes. robotics-render only pulls.

push() must be called only AFTER the DuckDB connection is closed (or an
explicit CHECKPOINT has run) — DuckDB folds the WAL into the main .duckdb
file on checkpoint, so the single file is self-consistent with no .wal.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("robotics.duckdb_sync")

# duckdb_path → GCS generation of the copy this process pulled (0 = the object
# didn't exist). push() uploads only if the remote is still that generation.
_PULLED_GENERATION: dict[str, int] = {}


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
        _PULLED_GENERATION[duckdb_path] = 0   # push must still find no object
        return {"pulled": False, "reason": "no_remote_object"}
    blob.reload()
    generation = blob.generation

    os.makedirs(os.path.dirname(duckdb_path) or ".", exist_ok=True)
    # Drop sidecar files left behind by a PREVIOUS run on this instance.
    # Cloud Run reuses warm containers: a run killed mid-transaction (e.g.
    # the request timeout) leaves a .wal describing the OLD file. Downloading
    # a fresh DB over it makes DuckDB replay that stale WAL on open ->
    # "Bitpacking offset is out of range ... corrupt database file", and the
    # whole run's writes are lost. Observed in prod 2026-08-26.
    for sidecar in (f"{duckdb_path}.wal", f"{duckdb_path}.tmp"):
        if os.path.exists(sidecar):
            log.warning("duckdb_pull removing stale sidecar %s", sidecar)
            try:
                os.remove(sidecar)
            except OSError as exc:
                log.warning("duckdb_pull could not remove %s: %s", sidecar, exc)
    blob.download_to_filename(duckdb_path, if_generation_match=generation)
    _PULLED_GENERATION[duckdb_path] = generation
    size = os.path.getsize(duckdb_path)
    log.info("duckdb_pull ok gs://%s/%s -> %s (%d bytes, generation %s)",
             bucket_name, obj, duckdb_path, size, generation)
    return {"pulled": True, "object": f"gs://{bucket_name}/{obj}", "bytes": size, "generation": generation}


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

    # NEVER overwrite the authoritative copy with a broken file. A run that
    # hit a fatal DuckDB error still reaches this call (push is non-fatal by
    # design), so verify the file opens and its core tables read before the
    # upload — otherwise prod state is destroyed by a bad local copy.
    try:
        import duckdb

        probe = duckdb.connect(duckdb_path, read_only=True)
        try:
            probe.execute("SELECT COUNT(*) FROM catalysts").fetchone()
            probe.execute("SELECT COUNT(*) FROM entities").fetchone()
        finally:
            probe.close()
    except Exception as exc:  # noqa: BLE001
        log.error("duckdb_push REFUSED — local DB failed integrity probe: %s", exc)
        return {"pushed": False, "reason": "integrity_probe_failed", "error": str(exc)}

    from google.cloud import storage

    obj = _object_name(duckdb_path)
    blob = storage.Client().bucket(bucket_name).blob(obj)
    from google.api_core.exceptions import PreconditionFailed

    expected = _PULLED_GENERATION.get(duckdb_path)
    try:
        if expected is None:   # never pulled in this process (tests / ad-hoc)
            blob.upload_from_filename(duckdb_path)
        else:
            blob.upload_from_filename(duckdb_path, if_generation_match=expected)
    except PreconditionFailed:
        log.error("duckdb_push CONFLICT — gs://%s/%s changed since pull (expected generation %s); "
                  "not overwriting. This run's DB changes are dropped; the next run redoes them.",
                  bucket_name, obj, expected)
        return {"pushed": False, "reason": "generation_conflict", "expected_generation": expected}
    _PULLED_GENERATION[duckdb_path] = blob.generation
    size = os.path.getsize(duckdb_path)
    log.info("duckdb_push ok %s -> gs://%s/%s (%d bytes)", duckdb_path, bucket_name, obj, size)
    return {"pushed": True, "object": f"gs://{bucket_name}/{obj}", "bytes": size}
