"""Entity cleanup: LLM-judged merges + corporate links (src/entities.py).

Works on the authoritative cloud DuckDB via src/duckdb_sync (generation-
pinned pull, generation-matched push: a concurrent ingest makes the push
fail instead of being overwritten — just re-run).

Modes (pick one):
    --audit                 LLM pass over every candidate group → JSONL report. Never writes.
    --apply-report FILE     apply a reviewed --audit report. No LLM: what runs is exactly what
                            was dry-run and reviewed.
    --merge "SRC=>DST"      admin: fold entity SRC into DST (exact entity names)
    --link "CHILD=>PARENT"  admin: record CHILD subsidiary_of PARENT
    --sister "A=>B"         admin: record A sister_of B

--apply-report applies merges at confidence >= --min-merge (0.85), links and
alias drops >= --min-link (0.80) / --min-merge; every less-certain claim becomes
an admin entry in admin_sessions/attention_needed/entries (resolved=false).
Names in the report that no longer exist (merged/renamed since) are skipped.
Admin modes always write (they are the admin's decision).

Usage: make entity-audit                       → report path printed
       make entity-apply REPORT=data/exports/entity_audit/entity_audit_….jsonl
       make entity-merge FROM="Hyundai Motors" INTO="Hyundai Motor Group"
       make entity-link CHILD="Hyundai Rotem" PARENT="Hyundai Motor Group" [REL=sister_of]
Then `make ingest-prod` to refresh Firestore cards + graph.
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

from src import db, duckdb_sync, entities  # noqa: E402
from src.config import load_config  # noqa: E402

_BATCH = 10   # candidate groups per LLM call


def _id(con, name: str) -> int:
    row = con.execute("SELECT entity_id FROM entities WHERE name = ?", [name]).fetchone()
    if not row:
        raise SystemExit(f"no entity named exactly {name!r}")
    return int(row[0])


def _pair(arg: str) -> tuple[str, str]:
    a, sep, b = arg.partition("=>")
    if not sep or not a.strip() or not b.strip():
        raise SystemExit(f"expected 'A=>B', got {arg!r}")
    return a.strip(), b.strip()


def plan(rows: list[dict], ids: dict[str, int], min_merge: float, min_link: float) -> dict:
    """Report rows → what to apply and what to send to review. Each claim must
    name a member of the row's own group and an entity that still exists."""
    merges: dict[int, tuple[int, float, str]] = {}
    links: list[tuple[int, int, str, float, str]] = []
    alias_drops: list[tuple[int, str]] = []
    review: list[dict] = []
    for r in rows:
        group, eid = set(r["group"]), ids.get(r["entity"])
        if eid is None:
            continue
        conf, reason = float(r["confidence"]), r["reason"]
        claims = []
        if r.get("same_as") in group and r["same_as"] != r["entity"]:
            claims.append(("merge", r["same_as"], min_merge))
        if r.get("parent") in group and r["parent"] != r["entity"]:
            claims.append(("subsidiary_of", r["parent"], min_link))
        claims += [("sister_of", s, min_link) for s in r.get("sister_of") or [] if s in group and s != r["entity"]]
        for kind, other, bar in claims:
            oid = ids.get(other)
            if oid is None:
                continue
            if conf < bar:
                review.append({"entity": r["entity"], "claim": kind, "other": other, "confidence": conf,
                               "reason": reason})
            elif kind == "merge":
                merges[eid] = (oid, conf, reason)
            else:
                links.append((eid, oid, kind, conf, reason))
        for alias in r.get("wrong_aliases") or []:
            if conf >= min_merge:
                alias_drops.append((eid, alias))
            else:
                review.append({"entity": r["entity"], "claim": "wrong_alias", "other": alias, "confidence": conf,
                               "reason": reason})
    return {"merges": merges, "final": entities.resolve_merges({s: d for s, (d, _, _) in merges.items()}),
            "links": links, "alias_drops": alias_drops, "review": review}


def _print_plan(p: dict, names: dict[int, str]) -> None:
    for s, d in p["final"].items():
        print(f"  merge  {names[s]}  =>  {names[d]}  ({p['merges'][s][1]:.2f}) {p['merges'][s][2][:90]}", file=sys.stderr)
    for c, par, rel, conf, _ in p["links"]:
        print(f"  link   {names[c]} --{rel}--> {names[par]}  ({conf:.2f})", file=sys.stderr)
    for eid, alias in p["alias_drops"]:
        print(f"  alias  drop {alias!r} from {names[eid]}", file=sys.stderr)
    for r in p["review"]:
        print(f"  review {r['entity']} {r['claim']} {r['other']} ({r['confidence']:.2f})", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--apply-report")
    mode.add_argument("--merge")
    mode.add_argument("--link")
    mode.add_argument("--sister")
    ap.add_argument("--min-merge", type=float, default=0.85)
    ap.add_argument("--min-link", type=float, default=0.80)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report-dir", default=str(_REPO_ROOT / "data" / "exports" / "entity_audit"))
    args = ap.parse_args()

    if not duckdb_sync.enabled():
        ap.error("DUCKDB_GCS_BUCKET is not set — this tool works on the authoritative cloud DB only")
    cfg = load_config()
    path = str(Path(tempfile.mkdtemp(prefix="entity_audit_")) / os.path.basename(cfg.duckdb_path))
    print(f"pulled {duckdb_sync.pull(path)}", file=sys.stderr)
    con = duckdb.connect(path)
    db.init_schema(con)
    names = dict(con.execute("SELECT entity_id, name FROM entities").fetchall())
    ids = {n: i for i, n in names.items()}

    if args.merge or args.link or args.sister:   # admin modes
        if args.merge:
            src, dst = _pair(args.merge)
            out = entities.merge(con, _id(con, src), _id(con, dst), confidence=None, reason="admin", source="admin")
            print(json.dumps({"merged": f"{src} => {dst}", **out}), file=sys.stderr)
        else:
            child, parent = _pair(args.link or args.sister)
            rel = "subsidiary_of" if args.link else "sister_of"
            entities.link(con, _id(con, child), _id(con, parent), rel, confidence=None, reason="admin", source="admin")
            print(json.dumps({"linked": f"{child} --{rel}--> {parent}"}), file=sys.stderr)
        con.execute("CHECKPOINT")
        con.close()
        return _push(path)

    if args.audit:   # LLM → report only
        groups = entities.candidate_groups(names)
        batches = [groups[i:i + _BATCH] for i in range(0, len(groups), _BATCH)]
        print(f"{len(groups)} candidate group(s), {len({e for g in groups for e in g})} entities, "
              f"{len(batches)} LLM call(s)", file=sys.stderr)
        from google import genai  # noqa: PLC0415
        client = genai.Client(api_key=cfg.extraction.api_key)

        def run(batch):
            try:
                return entities.judge(client, con.cursor(), model=cfg.extraction.model,
                                      thinking_level=cfg.extraction.direction_thinking_level, batch=batch), None
            except Exception as exc:  # noqa: BLE001
                return {}, f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(run, batches))
        rows, errors = [], 0
        for batch, (verdicts, err) in zip(batches, results):
            if err:
                errors += 1
                print(f"  ERROR batch: {err}", file=sys.stderr)
                continue
            for gi, group in enumerate(batch):
                members = sorted(names[e] for e in group)
                rows += [{"entity": v.name, **v.model_dump(exclude={"name"}), "group": members}
                         for v in verdicts.get(gi, []) if v.name in members]
        os.makedirs(args.report_dir, exist_ok=True)
        report = Path(args.report_dir) / f"entity_audit_{datetime.now(timezone.utc):%Y%m%dT%H%MZ}.jsonl"
        report.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        p = plan(rows, ids, args.min_merge, args.min_link)
        _print_plan(p, names)
        print(json.dumps({"groups": len(groups), "errors": errors, "merges": len(p["final"]),
                          "links": len(p["links"]), "alias_drops": len(p["alias_drops"]),
                          "for_review": len(p["review"]), "report": str(report)}), file=sys.stderr)
        print(f"dry run — review the report, then: make entity-apply REPORT={report.relative_to(_REPO_ROOT)}",
              file=sys.stderr)
        con.close()
        return 0

    # --apply-report: replay a reviewed report, no LLM.
    rows = [json.loads(line) for line in Path(args.apply_report).read_text().splitlines() if line.strip()]
    p = plan(rows, ids, args.min_merge, args.min_link)
    _print_plan(p, names)
    final = p["final"]
    for s, d in final.items():
        entities.merge(con, s, d, confidence=p["merges"][s][1], reason=p["merges"][s][2], source="entity_audit")
    for c, par, rel, conf, reason in p["links"]:
        c, par = final.get(c, c), final.get(par, par)
        if c not in final and par not in final:
            entities.link(con, c, par, rel, confidence=conf, reason=reason, source="entity_audit")
    for eid, alias in p["alias_drops"]:
        con.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?", [final.get(eid, eid), alias])
    if p["review"]:
        from google.cloud import firestore  # noqa: PLC0415

        from src.upstream_dedup import flag_attention  # noqa: PLC0415
        fs = firestore.Client(project=cfg.firestore.project or None)
        for r in p["review"]:   # one entry per (entity, other, claim) — several claims per entity are common
            flag_attention(fs, f"{r['entity']}~{r['other']}", f"entity_{r['claim']}",
                           f"Entity audit is unsure: {r['entity']} {r['claim'].replace('_', ' ')} {r['other']} "
                           f"(confidence {r['confidence']:.2f}). {r['reason']} "
                           f"If correct, apply with make entity-merge / entity-link.",
                           entity=r["entity"], other=r["other"], confidence=r["confidence"], category=cfg.sector)
    con.execute("CHECKPOINT")
    con.close()
    print(json.dumps({"merges": len(final), "links": len(p["links"]), "alias_drops": len(p["alias_drops"]),
                      "for_review": len(p["review"]), "report": args.apply_report}), file=sys.stderr)
    return _push(path)


def _push(path: str) -> int:
    out = duckdb_sync.push(path)
    if out.get("pushed"):
        print(f"pushed {out['object']}. Run `make ingest-prod` to refresh Firestore cards + graph.", file=sys.stderr)
        return 0
    print(f"NOT APPLIED ({out.get('reason')}): nothing was overwritten — re-run the same command.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
