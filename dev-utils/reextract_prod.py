"""Re-extract specific cards against the authoritative cloud DuckDB.

Same path as a cloud ingest (src/ingest.py): extract() with the direction
check, the LLM entity-resolution tier, write_extraction() in one
transaction. Each card's old rows are replaced. The DB goes through
src/duckdb_sync (generation-pinned pull, generation-matched push), so a
concurrent ingest makes the push fail instead of being overwritten.

Usage: make reextract-prod IDS=ROB-…,ROB-…   (dry run: runs the real write path on the
                                             pulled local copy and prints old vs new
                                             edges; only the upload is skipped)
       make reextract-prod IDS=… APPLY=1      (writes; then make ingest-prod)
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

from src import db, direction, duckdb_sync  # noqa: E402
from src.config import load_config  # noqa: E402
from src.extract import extract  # noqa: E402
from src.ingest import _decide_entities, _firestore_client, flag_unsure, write_extraction  # noqa: E402


def _edges(con, entry_id: str) -> list[str]:
    return sorted(r[0] for r in con.execute("""
        SELECT ea.name || ' --' || r.rel_type || '--> ' || eb.name FROM relationships r
        JOIN catalysts c USING (catalyst_id)
        JOIN entities ea ON ea.entity_id = r.entity_a_id JOIN entities eb ON eb.entity_id = r.entity_b_id
        WHERE c.entry_id = ? AND r.status IN ('active', 'materialized')""", [entry_id]).fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", required=True, help="comma-separated entry ids, or @file")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    raw = Path(args.ids[1:]).read_text() if args.ids.startswith("@") else args.ids
    ids = [i.strip() for i in raw.replace("\n", ",").split(",") if i.strip()]
    if not duckdb_sync.enabled():
        ap.error("DUCKDB_GCS_BUCKET is not set — this tool works on the authoritative cloud DB only")

    cfg = load_config()
    path = str(Path(tempfile.mkdtemp(prefix="reextract_")) / os.path.basename(cfg.duckdb_path))
    print(f"pulled {duckdb_sync.pull(path)}", file=sys.stderr)
    con = duckdb.connect(path)
    db.init_schema(con)
    findings = _firestore_client(cfg).collection(cfg.firestore.collection)
    lookup = direction.on_record_lookup(con)
    done = 0
    pending_flags = []   # admin entries go out only after a successful push
    for eid in ids:
        doc = findings.document(eid).get()
        if not doc.exists:
            print(json.dumps({"entry_id": eid, "skipped": "not in Arboryx findings"}), file=sys.stderr)
            continue
        entry = doc.to_dict()
        entry.pop("_synced_at", None)
        entry.pop("_hash", None)
        entry["source_url"] = db.clean_url(entry.get("source_url"))
        before = _edges(con, eid)
        result = extract(entry, cfg, on_record=lookup)
        decisions = _decide_entities(con, cfg, entry, result, flag=False)
        # The real write path, on the pulled LOCAL copy, in dry runs too: the
        # "after" edges are what resolution actually did (no simulation to drift).
        # Separate statements (DuckDB FK limit); any failure raises before the push.
        db.delete_by_entry_id(con, eid)
        write_extraction(con, entry, result, cfg, decisions)
        done += 1
        pending_flags.append((entry, decisions))
        print(json.dumps({"entry_id": eid, "before": before, "after": _edges(con, eid)}, ensure_ascii=False),
              file=sys.stderr)
    con.execute("CHECKPOINT")
    con.close()
    if not args.apply:
        print("dry run — local copy only, nothing uploaded (add APPLY=1). APPLY re-runs extraction, "
              "so its output can differ slightly from this preview.", file=sys.stderr)
        return 0
    out = duckdb_sync.push(path)
    if not out.get("pushed"):
        print(f"NOT APPLIED ({out.get('reason')}): nothing was overwritten — re-run the same command.", file=sys.stderr)
        return 2
    for entry, decisions in pending_flags:
        flag_unsure(cfg, entry, decisions)
    print(f"re-extracted {done} card(s); pushed {out['object']}. Run `make ingest-prod` to refresh Firestore.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
