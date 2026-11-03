"""Re-check the direction of stored directional relationships (one-time repair + top-ups).

Same verifier as ingest Pass 3 (src/direction.py), run over cards already in
the authoritative DuckDB (gs://$DUCKDB_GCS_BUCKET/robotics.duckdb).

Selection (one required — there is deliberately no "all"):
    --id ROB-…  /  --ids a,b,c      specific cards
    --from YYYY-MM-DD --to YYYY-MM-DD  cards in a date range
    --missing                       cards never direction-checked (no direction_audits row)
    --rel-ids 1,2,…  /  --rel-ids @file   only these edges (e.g. a report's 'unclear' ones);
                                    their cards' audit rows are left as they are
Options: --fetch-article  give the verifier the card's source article too (more
context for edges the short card text can't decide).
         --reject-unclear (with --rel-ids only) edges still 'unclear' get
status='rejected' — excluded by every card/graph/stats reader, row kept,
reversible. For edges the
first pass couldn't place because the edge itself is wrong (e.g. a
mis-resolved entity: "Audi acquires HUMAIN" from a Saudi Aramco story).

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


def _select(con, sector: str, args) -> list[tuple[int, str, str, str | None]]:
    where, params = ["c.sector = ?"], [sector]
    if args.rel_ids:
        where.append(f"c.catalyst_id IN (SELECT catalyst_id FROM relationships WHERE rel_id IN "
                     f"({','.join('?' * len(args.rel_ids))}))"); params += sorted(args.rel_ids)
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        where.append(f"c.entry_id IN ({','.join('?' * len(ids))})"); params += ids
    if args.date_from:
        where.append("c.timestamp >= ?"); params.append(args.date_from)
    if args.date_to:
        where.append("c.timestamp <= ?"); params.append(args.date_to)
    if args.missing:
        where.append("c.entry_id NOT IN (SELECT entry_id FROM direction_audits)")
    return con.execute(f"""SELECT c.catalyst_id, c.entry_id, c.headline || '. ' || c.raw_finding, c.source_url
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
    ap.add_argument("--rel-ids")
    ap.add_argument("--fetch-article", action="store_true")
    ap.add_argument("--reject-unclear", action="store_true")
    ap.add_argument("--dedupe-all", action="store_true",
                    help="no LLM: drop duplicate rows (same card, a, rel, b) across the whole DB")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--report-dir", default=str(_REPO_ROOT / "data" / "exports" / "direction_audit"))
    args = ap.parse_args()
    args.ids = ",".join(x for x in (args.id, args.ids) if x)
    raw = args.rel_ids or ""
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    args.rel_ids = {int(x) for x in raw.replace("\n", ",").split(",") if x.strip()}
    if args.reject_unclear and not args.rel_ids:
        ap.error("--reject-unclear needs --rel-ids (a reviewed list of edges, never a whole range)")
    if not (args.ids or args.date_from or args.date_to or args.missing or args.rel_ids or args.dedupe_all):
        ap.error("pick cards: --id/--ids, --from/--to, --missing or --rel-ids (there is no 'all')")

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
    if args.dedupe_all and not (args.ids or args.date_from or args.date_to or args.missing or args.rel_ids):
        # Deterministic sweep only — no card selection, no LLM.
        n = direction.dedupe_rows(con, None) if args.apply else con.execute("""
            SELECT COUNT(*) FROM (SELECT ROW_NUMBER() OVER (PARTITION BY catalyst_id, entity_a_id, rel_type,
                                  entity_b_id ORDER BY confidence DESC, rel_id) AS rn
                                  FROM relationships WHERE status IN ('active', 'materialized')) WHERE rn > 1
            """).fetchone()[0]
        print(json.dumps({"dedupe_only": True, "duplicate_rows": n, "applied": args.apply}), file=sys.stderr)
        con.execute("CHECKPOINT")
        con.close()
        if not args.apply:
            print("dry run — nothing written (add APPLY=1)", file=sys.stderr)
            return 0
        return _push(work, blob, generation, bucket)

    rules = direction.directional_types(cfg.vocab)
    lookup = direction.on_record_lookup(con)
    cards = []
    for cid, eid, text, url in _select(con, cfg.sector, args):
        edges = _edges(con, cid, rules)
        if args.rel_ids:
            edges = [e for e in edges if e["rel_id"] in args.rel_ids]
        names = sorted({n for e in edges for n in (e["entity_a"], e["entity_b"])})
        article = ""
        if args.fetch_article and url and edges:
            from src.fetch import fetch_article  # noqa: PLC0415
            fr = fetch_article(url, cfg.extraction.url_fetch)
            article = fr.text if fr.ok else ""
        cards.append((cid, eid, text, edges, lookup(names) if edges else [], article))
    print(f"{len(cards)} card(s) selected, {sum(len(c[3]) for c in cards)} directional edge(s)", file=sys.stderr)

    client = genai.Client(api_key=cfg.extraction.api_key)

    def run(card):
        cid, eid, text, edges, record, article = card
        if not edges:
            return eid, {}, None
        try:
            return eid, direction.check(client, model=cfg.extraction.model,
                                        thinking_level=cfg.extraction.direction_thinking_level,
                                        finding=text, article=article, edges=edges, rules=rules,
                                        on_record=record), None
        except Exception as exc:  # noqa: BLE001
            return eid, {}, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = {eid: (verdicts, err) for eid, verdicts, err in pool.map(run, cards)}

    os.makedirs(args.report_dir, exist_ok=True)
    report = Path(args.report_dir) / f"direction_audit_{datetime.now(timezone.utc):%Y%m%dT%H%MZ}.jsonl"
    totals = {"cards": 0, "checked": 0, "flipped": 0, "unclear": 0, "errors": 0}
    with report.open("w") as fh:
        for cid, eid, text, edges, record, _article in cards:
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
                    if args.apply and args.reject_unclear:
                        con.execute("UPDATE relationships SET status = 'rejected' WHERE rel_id = ?", [e["rel_id"]])
                if v and v.verdict == "flip":
                    stats["flipped"] += 1
                    if args.apply:   # RHS reads the pre-update row, so this swaps
                        con.execute("UPDATE relationships SET entity_a_id = entity_b_id, entity_b_id = entity_a_id "
                                    "WHERE rel_id = ?", [e["rel_id"]])
            # partial → stays MISSING for a re-run; --rel-ids re-checks a subset
            # of an already-audited card, so its audit row stays as it was.
            if args.apply and not stats["missing"] and not args.rel_ids:
                con.execute("INSERT OR REPLACE INTO direction_audits (entry_id, model, checked, flipped, unclear, source) "
                            "VALUES (?, ?, ?, ?, ?, 'audit')",
                            [eid, cfg.extraction.model, stats["checked"], stats["flipped"], stats["unclear"]])
            totals["cards"] += 1
            for k in ("checked", "flipped", "unclear"):
                totals[k] += stats[k]
    if args.apply:   # a flip can duplicate another row on the same card
        totals["deduped"] = direction.dedupe_rows(con, None if args.dedupe_all else [c[0] for c in cards])
    print(json.dumps({**totals, "report": str(report), "applied": args.apply}), file=sys.stderr)

    con.execute("CHECKPOINT")
    con.close()
    if not args.apply:
        print("dry run — nothing written (add APPLY=1)", file=sys.stderr)
        return 0
    if totals["errors"]:
        print(f"note: {totals['errors']} card(s) errored and were left unaudited; re-run with --missing", file=sys.stderr)
    return _push(work, blob, generation, bucket)


def _push(work: Path, blob, generation: int, bucket: str) -> int:
    """Integrity probe (same guard as duckdb_sync.push), then a generation-
    matched upload: a concurrent writer makes this fail, never overwritten."""
    probe = duckdb.connect(str(work), read_only=True)
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
