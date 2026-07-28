"""robotics-ingest — HTTP-triggered ingestion pipeline.

Local: `functions-framework --target=run_ingest` → POST http://localhost:8080/
Prod : Cloud Functions Gen 2 with same entry point, triggered by Cloud Scheduler.

Request payload (JSON, all optional):
    {
        "sector": "Robotics",     # default from env / config
        "dry_run": false,         # if true: read + report counts, skip LLM + writes
        "limit": null             # if >0: process at most N oldest-after-watermark entries
    }

Thin HTTP wrapper around src.ingest + src.export. All heavy lifting lives
in src/, so dev-utils/ CLIs can reuse the same code paths.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import functions_framework
from flask import Request

# Ensure /app is on sys.path (Cloud Functions Gen 2 auto-adds source dir, but
# local Docker WORKDIR=/app already does too — belt-and-suspenders).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import load_config  # noqa: E402
from src.duckdb_sync import pull as duckdb_pull, push as duckdb_push  # noqa: E402
from src.export import export_cards  # noqa: E402
from src.firestore_export import export_to_firestore  # noqa: E402
from src.ingest import run_ingest as run_ingest_pipeline  # noqa: E402


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("robotics-ingest")


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, **fields}, default=str))


def _publish_ingest_done(sector: str, written: int) -> dict[str, Any]:
    """Publish a completion event to the ingest-done topic so robotics-render
    fans out (push subscription → /render-batch).

    Never raises — the ingest already committed its writes, so a publish
    failure must not turn the response into an error. Returns a small summary
    for the response payload.
    """
    topic_name = os.environ.get("PUBSUB_DONE_TOPIC", "").strip()
    if not topic_name:
        # Local default: topic unset, nothing to fan out to.
        _log("publish_skipped", reason="topic_unset")
        return {"ok": True, "skipped": "topic_unset"}
    if written <= 0:
        _log("publish_skipped", reason="no_new_catalysts", written=written)
        return {"ok": True, "skipped": "no_new_catalysts"}

    try:
        project = os.environ.get("GCP_PROJECT", "").strip()
        if not project:
            import google.auth

            _, project = google.auth.default()
        from google.cloud import pubsub_v1

        topic_path = f"projects/{project}/topics/{topic_name}"
        data = json.dumps({
            "sector": sector,
            "written": written,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).encode("utf-8")
        future = pubsub_v1.PublisherClient().publish(topic_path, data)
        message_id = future.result(timeout=10)
        _log("publish_done", topic=topic_path, message_id=message_id, written=written)
        return {"ok": True, "message_id": message_id}
    except Exception as exc:
        _log("publish_failed", error=str(exc), error_type=type(exc).__name__)
        return {"ok": False, "error": str(exc)}


@functions_framework.http
def run_ingest(request: Request):
    start = time.monotonic()
    try:
        params = request.get_json(silent=True) or {}
    except Exception:
        params = {}

    dry_run = bool(params.get("dry_run", False))
    limit_raw = params.get("limit")
    limit = int(limit_raw) if limit_raw not in (None, "", 0, "0") else None

    cfg = load_config()
    if "sector" in params and params["sector"]:
        # Re-load with SECTOR env override; simpler than plumbing in.
        os.environ["SECTOR"] = str(params["sector"])
        cfg = load_config()

    _log("ingest_start", sector=cfg.sector, dry_run=dry_run, limit=limit)

    # Prod: copy the DuckDB file down from GCS before the run. No-op locally
    # (DUCKDB_GCS_BUCKET unset). A real GCS error is fatal — proceeding would
    # silently re-ingest everything into an empty DB.
    try:
        pull_summary = duckdb_pull(cfg.duckdb_path)
        _log("duckdb_pull", **pull_summary)
    except Exception as exc:
        _log("duckdb_pull_failed", error=str(exc), error_type=type(exc).__name__)
        return {"ok": False, "error": f"duckdb pull failed: {exc}"}, 500

    try:
        ingest_result = run_ingest_pipeline(cfg, dry_run=dry_run, limit=limit)
    except Exception as exc:
        _log("ingest_failed", error=str(exc), error_type=type(exc).__name__)
        return {"ok": False, "error": str(exc)}, 500

    export_summary = None
    sweep_summary = None
    firestore_summary = None
    duckdb_push_summary = None
    publish_summary = None
    if not dry_run:
        # Handles sweep for newly discovered entities — BEFORE export so the
        # fresh handles land on this run's cards. Prod mirror of the local
        # `make ingest` → sweep → export chain. Gated by env; budget-guarded
        # and non-fatal inside run_sweep (ingest must never fail because
        # handle resolution couldn't run).
        if os.environ.get("HANDLE_SWEEP_ENABLED", "").lower() == "true":
            from src.handle_sweep import run_sweep  # noqa: PLC0415

            sweep_summary = run_sweep(
                cfg.duckdb_path,
                limit=int(os.environ.get("HANDLE_SWEEP_LIMIT", "25") or 25),
            )
            _log("handle_sweep_done", **{k: v for k, v in sweep_summary.items()
                                         if k != "budget"})
        try:
            export_summary = export_cards(cfg)
            _log("export_done", **export_summary)
        except Exception as exc:
            _log("export_failed", error=str(exc), error_type=type(exc).__name__)
            # Export failure shouldn't fail the whole request — extraction already succeeded.
            export_summary = {"error": str(exc)}

        # Firestore + Firebase Storage push (gated by config flags).
        # Local dev keeps both off; prod flips them on. Failures here are
        # non-fatal — local cards.json is already written above.
        try:
            firestore_summary = export_to_firestore(cfg)
            _log("firestore_export_done", **firestore_summary)
        except Exception as exc:
            _log("firestore_export_failed", error=str(exc), error_type=type(exc).__name__)
            firestore_summary = {"error": str(exc)}

        # Prod: copy the updated DuckDB file back to GCS. No-op locally.
        # Non-fatal — extraction + Firestore writes already succeeded. A
        # failure here just means the next run re-pulls the older DB and
        # re-ingests this batch (wasteful, not corrupting); operators catch
        # it via the `duckdb_push_failed` log line.
        try:
            duckdb_push_summary = duckdb_push(cfg.duckdb_path)
            _log("duckdb_push", **duckdb_push_summary)
        except Exception as exc:
            _log("duckdb_push_failed", error=str(exc), error_type=type(exc).__name__)
            duckdb_push_summary = {"error": str(exc)}

        publish_summary = _publish_ingest_done(cfg.sector, ingest_result.entries_written)

    payload = {
        "ok": True,
        "duration_s": round(time.monotonic() - start, 2),
        **ingest_result.to_json(),
        "handle_sweep": sweep_summary,
        "export": export_summary,
        "firestore_export": firestore_summary,
        "duckdb_push": duckdb_push_summary,
        "publish": publish_summary,
    }
    _log("ingest_done", **payload)
    return payload, 200


if __name__ == "__main__":
    # Ad-hoc: `python main.py` runs the same HTTP handler on :8080.
    from functions_framework import create_app

    app = create_app(target="run_ingest")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
