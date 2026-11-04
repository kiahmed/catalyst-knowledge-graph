"""Backfill / repair card embeddings in Firestore (src/embed.py, src/embed_sync.py).

Reads the authoritative cloud DuckDB (pull only — never pushes) and writes
`embedding` + filter fields onto existing CKG card docs.

Selection (one required — deliberately no "all"):
    --card ROB-…  /  --ids a,b,c       specific cards (re-embedded even if current)
    --from YYYY-MM-DD [--to …]         cards in a date range (re-embedded even if current)
    --missing                          every card with no embedding or stale text
                                       (with --ids/--from/--to: only the stale ones in that selection)
--dry-run: count only, no embed calls.

Usage: make embed CARD=ROB-… | FROM=… TO=… | MISSING=1 [DRY=1]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)

import duckdb  # noqa: E402

from src import duckdb_sync, embed_sync  # noqa: E402
from src.config import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card"); ap.add_argument("--ids")
    ap.add_argument("--from", dest="date_from"); ap.add_argument("--to", dest="date_to")
    ap.add_argument("--missing", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ids = [i.strip() for i in ",".join(x for x in (args.card, args.ids) if x).split(",") if i.strip()]
    if not (ids or args.date_from or args.date_to or args.missing):
        ap.error("pick cards: --card/--ids, --from/--to, or --missing (there is no 'all')")
    if not duckdb_sync.enabled():
        ap.error("DUCKDB_GCS_BUCKET is not set — this tool reads the authoritative cloud DB")

    cfg = load_config()
    path = str(Path(tempfile.mkdtemp(prefix="embed_")) / os.path.basename(cfg.duckdb_path))
    duckdb_sync.pull(path)
    con = duckdb.connect(path, read_only=True)
    entry_ids: list[str] | None = None
    if ids or args.date_from or args.date_to:
        where, params = ["sector = ?"], [cfg.sector]
        if ids:
            where.append(f"entry_id IN ({','.join('?' * len(ids))})"); params += ids
        if args.date_from:
            where.append("timestamp >= ?"); params.append(args.date_from)
        if args.date_to:
            where.append("timestamp <= ?"); params.append(args.date_to)
        entry_ids = [r[0] for r in con.execute(f"SELECT entry_id FROM catalysts WHERE {' AND '.join(where)}",
                                               params).fetchall()]

    from google import genai  # noqa: PLC0415
    from google.cloud import firestore  # noqa: PLC0415

    db_kw = {"database": cfg.firestore.database} if cfg.firestore.database not in ("", "(default)") else {}
    fs = firestore.Client(project=cfg.firestore.project or None, **db_kw)
    items = fs.collection(cfg.firestore_export.collection).document(cfg.firestore_export.catalysts_subpath) \
        .collection("items")
    out = embed_sync.sync(con, cfg.sector, fs, items, genai.Client(api_key=cfg.extraction.api_key), entry_ids,
                          only_stale=args.missing or entry_ids is None, dry_run=args.dry_run)
    con.close()
    print(json.dumps({**out, "dry_run": args.dry_run}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
