"""Re-run the extractor on a single master-log entry.

Why this exists: when you spot-check a backtest dump and disagree with
the triples for some entry, you iterate the prompt/vocab/config and fire
this CLI at just that entry_id. No full batch re-run needed.

Usage (from repo root, inside the robotics-ingest container recommended):
    docker compose exec robotics-ingest python /app/dev-utils/reextract.py ROB-041726-001
    docker compose exec robotics-ingest python /app/dev-utils/reextract.py ROB-041726-001 --write

Flags:
    --write            Delete-then-insert the entry's rows in DuckDB (otherwise dry).
    --from-firestore   Force re-fetch from Arboryx Firestore (default: try DuckDB first, fall back to Firestore).
    --prompt-version   Label this run (stored on catalysts.prompt_version). Default = config.
    --raw              Dump the raw Gemini response text instead of our parsed view.

Outputs to stdout as pretty JSON.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Make `import src.X` work whether invoked from repo root or inside /app.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Route retry warnings + extract errors to stderr so the CLI's JSON on
# stdout stays clean but operators still see transient retries / MAX_TOKENS
# bumps / extract_fail lines.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402
from src.extract import extract  # noqa: E402
from src.ingest import write_extraction  # noqa: E402


def _find_entry_in_db(con, entry_id: str) -> dict[str, Any] | None:
    """Reconstruct the master-log shape from DuckDB so we don't need GCS."""
    row = con.execute(
        """
        SELECT entry_id, sector, timestamp, source_url, raw_finding,
               sentiment_takeaways, guidance_play, price_levels
        FROM catalysts WHERE entry_id = ?
        """,
        [entry_id],
    ).fetchone()
    if not row:
        return None
    (eid, sector, ts, url, finding, takeaways, guide, levels) = row
    return {
        "entry_id": eid,
        "category": sector,
        "timestamp": ts.isoformat() if ts else None,
        "source_url": url,
        "finding": finding,
        "sentiment_takeaways": takeaways,
        "guidance_play": guide,
        "price_levels": levels,
    }


def _find_entry_in_firestore(entry_id: str, cfg) -> dict[str, Any] | None:
    """Single-doc fetch by entry_id (collection key). One round trip."""
    from src.ingest import _firestore_client  # reuse client construction

    client = _firestore_client(cfg)
    snap = client.collection(cfg.firestore.collection).document(entry_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    d.pop("_synced_at", None)
    d.pop("_hash", None)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-extract one entry.")
    ap.add_argument("entry_id")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--from-firestore", action="store_true")
    ap.add_argument("--prompt-version", default=None)
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if args.prompt_version:
        # Swap the prompt_version for this run only (frozen dataclass — rebuild it).
        from dataclasses import replace
        cfg = replace(cfg, extraction=replace(cfg.extraction, prompt_version=args.prompt_version))

    con = db.connect(cfg.duckdb_path)
    try:
        db.init_schema(con)

        entry: dict[str, Any] | None = None
        if not args.from_firestore:
            entry = _find_entry_in_db(con, args.entry_id)
        if entry is None:
            entry = _find_entry_in_firestore(args.entry_id, cfg)
        if entry is None:
            print(f"entry_id {args.entry_id!r} not found in DuckDB or GCS", file=sys.stderr)
            return 2

        result = extract(entry, cfg)

        if args.raw:
            print(result.raw_response_json)
            return 0

        out = {
            "entry_id": args.entry_id,
            "prompt_version": result.prompt_version,
            "model": result.model,
            "reasoning": result.reasoning,
            "headline": result.headline,
            "sentiment_label": result.sentiment_label,
            "entities": result.entities,
            "relationships": result.relationships,
            "flagged_medium_confidence": result.flagged_medium_confidence,
            "dropped_low_confidence": result.dropped_low_confidence,
            "wrote_to_db": False,
        }

        if args.write:
            db.delete_by_entry_id(con, args.entry_id)
            catalyst_id = write_extraction(con, entry, result, cfg)
            out["wrote_to_db"] = True
            out["catalyst_id"] = catalyst_id

        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
