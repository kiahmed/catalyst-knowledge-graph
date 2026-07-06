"""Backtest the extractor across a range of master-log entries.

Read-only: does NOT touch DuckDB. Writes a JSONL dump to
`/data/exports/backtests/backtest_<sector>_<prompt_version>_<YYYYMMDDTHHMMZ>.jsonl`
so you can eyeball LLM outputs before committing a prompt change.

Each line in the JSONL is one entry with (input, extraction) — easy to diff
between prompt_version runs with standard tools.

Usage:
    docker compose exec robotics-ingest python /app/dev-utils/backtest.py
    docker compose exec robotics-ingest python /app/dev-utils/backtest.py --limit 20
    docker compose exec robotics-ingest python /app/dev-utils/backtest.py --ids ROB-041726-001,ROB-041726-002
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)

from src.config import load_config  # noqa: E402
from src.extract import extract  # noqa: E402


def _fetch_findings(cfg, sector: str) -> list[dict[str, Any]]:
    """Read all findings for ``sector`` from Arboryx Firestore, newest-first
    (matches the legacy GCS master-log ordering contract)."""
    from google.cloud import firestore as _fs
    from google.cloud.firestore_v1.base_query import FieldFilter
    from src.ingest import _firestore_client

    client = _firestore_client(cfg)
    q = (
        client.collection(cfg.firestore.collection)
        .where(filter=FieldFilter("category", "==", sector))
        .order_by("timestamp", direction=_fs.Query.DESCENDING)
        .order_by("entry_id", direction=_fs.Query.DESCENDING)
    )
    out: list[dict[str, Any]] = []
    for doc in q.stream():
        d = doc.to_dict() or {}
        d.pop("_synced_at", None)
        d.pop("_hash", None)
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest extractor on master-log entries.")
    ap.add_argument("--limit", type=int, default=None, help="Only process the N most recent entries.")
    ap.add_argument("--ids", type=str, default=None, help="Comma-separated list of entry_ids to process.")
    ap.add_argument("--sector", type=str, default=None, help="Override sector filter (default from config).")
    ap.add_argument("--output", type=str, default=None, help="Override output path.")
    args = ap.parse_args()

    cfg = load_config()
    sector = args.sector or cfg.sector

    pool = _fetch_findings(cfg, sector)

    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",") if s.strip()}
        selected = [e for e in pool if e.get("entry_id") in wanted]
    else:
        # Firestore query already ordered newest-first.
        selected = pool[: args.limit] if args.limit else pool

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    default_name = f"backtest_{sector}_{cfg.extraction.prompt_version}_{stamp}.jsonl"
    out_path = args.output or os.path.join(cfg.backtest_dir, default_name)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print(f"backtest: {len(selected)} entries → {out_path}", file=sys.stderr)

    errors = 0
    with open(out_path, "w") as f:
        for i, e in enumerate(selected, 1):
            entry_id = e.get("entry_id") or "?"
            t0 = time.monotonic()
            try:
                result = extract(e, cfg)
                record = {
                    "entry_id": entry_id,
                    "timestamp": e.get("timestamp"),
                    "prompt_version": result.prompt_version,
                    "model": result.model,
                    "duration_s": round(time.monotonic() - t0, 2),
                    "input": {
                        "finding": e.get("finding"),
                        "sentiment_takeaways": e.get("sentiment_takeaways"),
                        "source_url": e.get("source_url"),
                    },
                    "extraction": {
                        "reasoning": result.reasoning,
                        "significance_score": result.significance_score,
                        "headline": result.headline,
                        "sentiment_label": result.sentiment_label,
                        "entities": result.entities,
                        "relationships": result.relationships,
                        "research_sources": result.research_sources,
                        "enrichment": result.enrichment,
                        "flagged_medium_confidence": result.flagged_medium_confidence,
                        "dropped_low_confidence": result.dropped_low_confidence,
                    },
                }
            except Exception as exc:
                errors += 1
                record = {
                    "entry_id": entry_id,
                    "timestamp": e.get("timestamp"),
                    "prompt_version": cfg.extraction.prompt_version,
                    "model": cfg.extraction.model,
                    "duration_s": round(time.monotonic() - t0, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            print(f"  [{i}/{len(selected)}] {entry_id} ({record.get('duration_s')}s)", file=sys.stderr)

    print(f"done. {len(selected) - errors} ok, {errors} errors. output: {out_path}", file=sys.stderr)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
