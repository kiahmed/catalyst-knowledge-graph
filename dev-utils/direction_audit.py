"""Re-check the direction of stored directional relationships (one-time repair + top-ups).

Same verifier as ingest Pass 3 (src/direction.py), run over cards already in
the authoritative DuckDB (gs://$DUCKDB_GCS_BUCKET/robotics.duckdb).

Selection (one required — there is deliberately no "all"):
    --id ROB-…  /  --ids a,b,c      specific cards
    --from YYYY-MM-DD --to YYYY-MM-DD  cards in a date range
    --missing                       cards never direction-checked (no direction_audits row)

Default is a dry run: verdicts go to a JSONL report, nothing is written.
--apply: flip → swap entity_a/entity_b on that row ('unclear' leaves it as
is); one direction_audits row per card. The DB is
uploaded with an if-generation-match precondition, so a cloud ingest that ran
meanwhile makes the upload fail instead of being overwritten — just re-run.
Firestore cards/graph pick the change up on the next ingest (make ingest-prod).

Usage: make direction-audit ID=ROB-… | FROM=… TO=… | MISSING=1  [APPLY=1]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)

import duckdb  # noqa: E402
from google import genai  # noqa: E402

from src import db, direction  # noqa: E402
from src.config import load_config  # noqa: E402


def _select(con, sector: str, args) -> list[tuple[int, str, str]]:
    where, params = ["c.sector = ?"], [sector]
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        where.append(f"c.entry_id IN ({','.join('?' * len(ids))})"); params += ids
    if args.date_from:
        where.append("c.timestamp >= ?"); params.append(args.date_from)
    if args.date_to:
        where.append("c.timestamp <= ?"); params.append(args.date_to)
    if args.missing:
        where.append("c.entry_id NOT IN (SELECT entry_id FROM direction_audits)")
    return con.execute(f"""SELECT c.catalyst_id, c.entry_id, c.headline || '. ' || c.raw_finding
                           FROM catalysts c WHERE {' AND '.join(where)} ORDER BY c.timestamp, c.entry_id""",
                       params).fetchall()


def _edges(con, catalyst_id: int, rules: dict[str, str]) -> list[dict]:
    marks = ",".join("?" * len(rules))
    rows = con.execute(f"""
        SELECT r.rel_id, ea.name, r.rel_type, eb.name, r.mechanism
        FROM relationships r JOIN entities ea ON ea.entity_id = r.entity_a_id
                             JOIN entities eb ON eb.entity_id = r.entity_b_id
        WHERE r.catalyst_id = ? AND r.entity_a_id <> r.entity_b_id
          AND r.status IN ('active', 'materialized') AND r.rel_type IN ({marks})
        ORDER BY r.rel_id""", [catalyst_id, *rules]).fetchall()
    return [{"rel_id": i, "entity_a": a, "rel_type": t, "entity_b": b, "mechanism": m} for i, a, t, b, m in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id"); ap.add_argument("--ids")
    ap.add_argument("--from", dest="date_from"); ap.add_argument("--to", dest="date_to")
    ap.add_argument("--missing", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--report-dir", default=str(_REPO_ROOT / "data" / "exports" / "direction_audit"))
    args = ap.parse_args()
    args.ids = ",".join(x for x in (args.id, args.ids) if x)
    if not (args.ids or args.date_from or args.date_to or args.missing):
        ap.error("pick cards: --id/--ids, --from/--to, or --missing (there is no 'all')")

    cfg = load_config()
    bucket = os.environ.get("DUCKDB_GCS_BUCKET", "").strip()
    if not bucket:
        ap.error("DUCKDB_GCS_BUCKET is not set — this tool works on the authoritative cloud DB only")
    from google.cloud import storage  # noqa: PLC0415

    blob = storage.Client().bucket(bucket).blob(os.path.basename(cfg.duckdb_path))
    work = Path(tempfile.mkdtemp(prefix="direction_audit_")) / "robotics.duckdb"
    blob.reload()
    generation = blob.generation
    blob.download_to_filename(str(work), if_generation_match=generation)
    print(f"pulled gs://{bucket}/{blob.name} generation={generation}", file=sys.stderr)

    con = duckdb.connect(str(work))
    db.init_schema(con)   # adds direction_audits on first use
    rules = direction.directional_types(cfg.vocab)
    lookup = direction.on_record_lookup(con)
    cards = []
    for cid, eid, text in _select(con, cfg.sector, args):
        edges = _edges(con, cid, rules)
        names = sorted({n for e in edges for n in (e["entity_a"], e["entity_b"])})
        cards.append((cid, eid, text, edges, lookup(names) if edges else []))
    print(f"{len(cards)} card(s) selected, {sum(len(c[3]) for c in cards)} directional edge(s)", file=sys.stderr)

    client = genai.Client(api_key=cfg.extraction.api_key)

    def run(card):
        cid, eid, text, edges, record = card
        if not edges:
            return eid, {}, None
        try:
            return eid, direction.check(client, model=cfg.extraction.model,
                                        thinking_level=cfg.extraction.direction_thinking_level,
                                        finding=text, article="", edges=edges, rules=rules,
                                        on_record=record), None
        except Exception as exc:  # noqa: BLE001
            return eid, {}, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = {eid: (verdicts, err) for eid, verdicts, err in pool.map(run, cards)}

    os.makedirs(args.report_dir, exist_ok=True)
    report = Path(args.report_dir) / f"direction_audit_{datetime.now(timezone.utc):%Y%m%dT%H%MZ}.jsonl"
    totals = {"cards": 0, "checked": 0, "flipped": 0, "unclear": 0, "errors": 0}
    with report.open("w") as fh:
        for cid, eid, text, edges, record in cards:
            verdicts, err = results[eid]
            if err:
                totals["errors"] += 1
                fh.write(json.dumps({"entry_id": eid, "error": err}) + "\n")
                continue
            stats = {"checked": len(edges), "flipped": 0, "unclear": 0, "missing": 0}
            for i, e in enumerate(edges):
                v = verdicts.get(i)
                row = {"entry_id": eid, "rel_id": e["rel_id"],
                       "edge": f"{e['entity_a']} --{e['rel_type']}--> {e['entity_b']}",
                       "verdict": v.verdict if v else "missing",
                       "statement": v.statement if v else None, "reason": v.reason if v else None}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                if v is None:
                    stats["missing"] += 1
                if v and v.verdict == "unclear":
                    stats["unclear"] += 1
                if v and v.verdict == "flip":
                    stats["flipped"] += 1
                    if args.apply:   # RHS reads the pre-update row, so this swaps
                        con.execute("UPDATE relationships SET entity_a_id = entity_b_id, entity_b_id = entity_a_id "
                                    "WHERE rel_id = ?", [e["rel_id"]])
            if args.apply and not stats["missing"]:   # partial → stays MISSING for a re-run
                con.execute("INSERT OR REPLACE INTO direction_audits (entry_id, model, checked, flipped, unclear, source) "
                            "VALUES (?, ?, ?, ?, ?, 'audit')",
                            [eid, cfg.extraction.model, stats["checked"], stats["flipped"], stats["unclear"]])
            totals["cards"] += 1
            for k in ("checked", "flipped", "unclear"):
                totals[k] += stats[k]
    print(json.dumps({**totals, "report": str(report), "applied": args.apply}), file=sys.stderr)

    con.execute("CHECKPOINT")
    con.close()
    if not args.apply:
        print("dry run — nothing written (add APPLY=1)", file=sys.stderr)
        return 0
    if totals["errors"]:
        print(f"note: {totals['errors']} card(s) errored and were left unaudited; re-run with --missing", file=sys.stderr)
    probe = duckdb.connect(str(work), read_only=True)   # same guard as duckdb_sync.push
    try:
        probe.execute("SELECT COUNT(*) FROM catalysts").fetchone()
        probe.execute("SELECT COUNT(*) FROM entities").fetchone()
    finally:
        probe.close()
    from google.api_core.exceptions import PreconditionFailed  # noqa: PLC0415

    try:
        blob.upload_from_filename(str(work), if_generation_match=generation)
    except PreconditionFailed:
        print(f"NOT APPLIED: gs://{bucket}/{blob.name} changed since the pull (an ingest ran). "
              f"Nothing was overwritten — re-run the same command.", file=sys.stderr)
        return 2
    print(f"pushed gs://{bucket}/{blob.name} (was generation {generation}). "
          f"Run `make ingest-prod` to refresh Firestore cards + graph.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
