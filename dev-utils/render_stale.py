"""Which card images are stale? — card data changed after its images were rendered.

A card's live Firestore doc (CKG-<sector>/catalysts/items/<id>) is rewritten only
when its content changes: ingest delta export, re-extraction, direction fixes,
merges, admin edits (written immediately). So the doc's update_time is "data last
changed", and each image's bucket `updated` is "image last rendered". A card is
stale when its doc changed after its card PNG or its graph poster, or when an
image is missing.

Read-only: no DuckDB, no LLM, no writes except the report in data/.
Caveat: a FIRESTORE_EXPORT_FULL_SYNC run rewrites every doc → everything
looks stale once.

Usage (via make): make render-stale [APPLY=1]
    --out-dir data   report data/render_stale_<UTC ts>.txt + .ids (comma list)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def summarize() -> int:
    """make render-stale APPLY=1: print one render-batch reply; exit 1 if anything failed."""
    import json  # noqa: PLC0415

    d = json.load(sys.stdin)
    print({k: d[k] for k in ("rendered", "failed", "uploaded", "upload_skipped", "graphs_rendered", "graphs_failed")
           if k in d})
    return 1 if (not d.get("ok") or d.get("failed") or d.get("graphs_failed")) else 0


def main() -> int:
    if "--summarize" in sys.argv:
        return summarize()
    ap = argparse.ArgumentParser()
    from google.cloud import firestore, storage  # noqa: PLC0415  (container only; --summarize runs on the host)

    ap.add_argument("--out-dir", default=str(_ROOT / "data"))
    ap.add_argument("--collection", default=os.environ.get("FIRESTORE_EXPORT_COLLECTION", "CKG-Robotics"))
    ap.add_argument("--bucket", default=os.environ.get("CARDS_BUCKET", "robotics-cards"))
    args = ap.parse_args()

    fs = firestore.Client(project=os.environ.get("GCP_PROJECT") or None)
    items = fs.collection(args.collection).document("catalysts").collection("items")
    docs = {d.id: (d.update_time, (d.to_dict() or {}).get("headline", ""))
            for d in items.select(["headline"]).stream()}

    bucket = storage.Client(project=os.environ.get("GCP_PROJECT") or None).bucket(args.bucket)

    def images(prefix: str) -> dict:
        return {b.name[len(prefix):-4]: b.updated for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".png")}

    cards, graphs = images("cards/"), images("graphs/")

    rows = []
    for cid, (changed, head) in sorted(docs.items()):
        why = []
        for label, imgs in (("card image", cards), ("graph poster", graphs)):
            at = imgs.get(cid)
            if at is None:
                why.append(f"{label} missing")
            elif changed > at:
                why.append(f"{label} older ({at:%Y-%m-%d %H:%M}Z)")
        if why:
            rows.append((cid, changed, "; ".join(why), head))

    now = datetime.now(timezone.utc)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"render_stale_{now:%Y%m%dT%H%MZ}"
    lines = [f"RENDER-STALE CARDS — {now:%Y-%m-%d %H:%M}Z (live Firestore card docs vs bucket {args.bucket})",
             "A card is listed when its data changed after its card image / graph poster, or an image is missing.",
             f"Cards checked: {len(docs)}  |  stale: {len(rows)}",
             "Re-render exactly these: make render-stale APPLY=1   (or the ids in the matching .ids file)", ""]
    lines += [f"{cid}  data changed {changed:%Y-%m-%d %H:%M}Z  |  {why}  |  {head[:90]}" for cid, changed, why, head in rows]
    Path(f"{base}.txt").write_text("\n".join(lines) + "\n")
    Path(f"{base}.ids").write_text(",".join(r[0] for r in rows) + "\n")
    print(f"{len(rows)} stale of {len(docs)} cards → {base}.txt", file=sys.stderr)
    print(os.path.relpath(f"{base}.ids", _ROOT))   # stdout: the ids file (repo-relative), for the make target
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
