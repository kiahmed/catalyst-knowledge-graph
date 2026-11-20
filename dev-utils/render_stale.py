"""Which card images are stale — and which images are orphaned?

Stale (precise): export writes `render_fp` into each card doc — a hash of exactly what the card's images draw
(src/render_fp.py) — and the render service stamps the hash it rendered from onto each image's metadata. A card is
stale when an image is missing, or its stamp differs from the doc's render_fp ("not fingerprinted" = rendered before
the fingerprint existed → once). Fields the images don't draw never flag a card.
Fallback for a doc exported before render_fp existed: the old time check (doc update_time newer than the image).

Orphaned: a card PNG / graph poster whose card no longer exists (deleted, or merged away by the Arboryx dedup).
Images of ids still listed in a live card's `merged_from` are KEPT — old share links of a merged duplicate keep
resolving. `make render-stale APPLY=1` deletes the orphans at the END, after the re-render succeeded; each id is
re-checked right before its own delete (a fresh read of that card doc + a merged_from query). Safety: no deletes when fewer than 100 live cards were
read, or when more than half of the images would go (that's a bad read, not cleanup).


Usage (via make): make render-stale [APPLY=1]
    report → data/render_stale.txt, ids → data/render_stale.ids, orphans → data/render_orphans.ids
    (fixed files, overwritten each run)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PREFIXES = ("cards/", "graphs/")
MIN_LIVE_CARDS = 100


def summarize() -> int:
    """make render-stale APPLY=1: print one render-batch reply; exit 1 if anything failed."""
    import json  # noqa: PLC0415

    d = json.load(sys.stdin)
    bad = ("failed", "graphs_failed", "upload_failed", "graphs_upload_failed")
    print({k: d[k] for k in ("rendered", "uploaded", "upload_skipped", "graphs_rendered", *bad, "error") if k in d})
    return 1 if (not d.get("ok") or any(d.get(k) for k in bad)) else 0


def _clients(args):
    from google.cloud import firestore, storage  # noqa: PLC0415  (container only; --summarize runs on the host)

    fs = firestore.Client(project=os.environ.get("GCP_PROJECT") or None)
    items = fs.collection(args.collection).document("catalysts").collection("items")
    bucket = storage.Client(project=os.environ.get("GCP_PROJECT") or None).bucket(args.bucket)
    return items, bucket


def _live(items) -> tuple[dict, set[str]]:
    """{card_id: (update_time, headline, render_fp)} and every id kept alive through a live card's merged_from."""
    docs, merged = {}, set()
    for d in items.select(["headline", "merged_from", "render_fp"]).stream():
        data = d.to_dict() or {}
        docs[d.id] = (d.update_time, data.get("headline", ""), data.get("render_fp"))
        merged.update(str(x) for x in (data.get("merged_from") or []))
    return docs, merged


def orphans_of(image_ids: set[str], live: set[str], merged: set[str]) -> list[str]:
    """Image ids with no live card, except removed duplicates still referenced by a live card's merged_from."""
    return sorted(i for i in image_ids if i not in live and i not in merged)


def stale_reason(label: str, doc_fp: str | None, changed, image) -> str | None:
    """Why one image of a card needs re-rendering, or None. image = (updated, stamped render_fp) or None (missing)."""
    if image is None:
        return f"{label} missing"
    at, img_fp = image
    if doc_fp:                                            # precise: drawn content vs what the image was drawn from
        if img_fp == doc_fp:
            return None
        return f"{label} not fingerprinted" if not img_fp else f"{label} content changed"
    return f"{label} older ({at:%Y-%m-%d %H:%M}Z)" if changed > at else None   # pre-render_fp doc


def safe_to_delete(n_live: int, n_images: int, n_orphans: int) -> tuple[bool, str]:
    if n_live < MIN_LIVE_CARDS:
        return False, f"only {n_live} live cards read (< {MIN_LIVE_CARDS}) — looks like a bad read"
    if n_images and n_orphans > n_images / 2:
        return False, f"{n_orphans} of {n_images} images would go (> half) — looks like a bad read"
    return True, ""


def still_orphan(items, cid: str) -> bool:
    """Fresh, per-id check made right before deleting: no live card doc with this id, and no live card lists it
    in merged_from."""
    if items.document(cid).get().exists:
        return False
    return not any(True for _ in items.where("merged_from", "array_contains", cid).limit(1).stream())


def delete_orphans(args, items=None, bucket=None) -> int:
    """make render-stale APPLY=1, last step: delete the orphan images listed in data/render_orphans.ids."""
    ids = [i for i in Path(args.delete_orphans).read_text().strip().split(",") if i]
    if not ids:
        print("orphans: none")
        return 0
    if items is None or bucket is None:
        items, bucket = _clients(args)
    docs, _merged = _live(items)                      # the guard needs the totals; each id is re-checked below
    image_ids = set()
    for prefix in _PREFIXES:
        image_ids |= {b.name[len(prefix):-4] for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".png")}
    ok, why = safe_to_delete(len(docs), len(image_ids), len(ids))
    if not ok:
        print(f"orphans: NOT deleted — {why}")
        return 1
    gone = 0
    for cid in ids:
        if not still_orphan(items, cid):              # per id, immediately before its delete
            print(f"orphans: kept {cid} (its card exists again)")
            continue
        for prefix in _PREFIXES:
            blob = bucket.blob(f"{prefix}{cid}.png")
            if blob.exists():
                blob.delete()
                gone += 1
    print(f"orphans: deleted {gone} image(s) of {len(ids)} orphaned card id(s)")
    return 0


def main() -> int:
    if "--summarize" in sys.argv:
        return summarize()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(_ROOT / "data"))
    ap.add_argument("--collection", default=os.environ.get("FIRESTORE_EXPORT_COLLECTION", "CKG-Robotics"))
    ap.add_argument("--bucket", default=os.environ.get("CARDS_BUCKET", "robotics-cards"))
    ap.add_argument("--delete-orphans", metavar="IDS_FILE", help="delete the orphan images listed in this file")
    args = ap.parse_args()
    if args.delete_orphans:
        return delete_orphans(args)

    items, bucket = _clients(args)
    docs, merged = _live(items)

    def images(prefix: str) -> dict:   # list_blobs returns custom metadata too — no per-blob reads
        return {b.name[len(prefix):-4]: (b.updated, (b.metadata or {}).get("render_fp"))
                for b in bucket.list_blobs(prefix=prefix) if b.name.endswith(".png")}

    cards, graphs = images("cards/"), images("graphs/")

    rows = []
    for cid, (changed, head, doc_fp) in sorted(docs.items()):
        why = [r for label, imgs in (("card image", cards), ("graph poster", graphs))
               if (r := stale_reason(label, doc_fp, changed, imgs.get(cid)))]
        if why:
            rows.append((cid, changed, "; ".join(why), head))

    orphans = orphans_of(set(cards) | set(graphs), set(docs), merged)
    ok, why_not = safe_to_delete(len(docs), len(set(cards) | set(graphs)), len(orphans))

    now = datetime.now(timezone.utc)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"RENDER-STALE CARDS — {now:%Y-%m-%d %H:%M}Z (live Firestore card docs vs bucket {args.bucket})",
             "A card is listed when what its images draw changed (render_fp), an image is missing, or it predates the "
             "fingerprint.",
             f"Cards checked: {len(docs)}  |  stale: {len(rows)}  |  orphaned images (card gone): {len(orphans)}",
             "Re-render these and delete the orphans: make render-stale APPLY=1", ""]
    lines += [f"{cid}  data changed {changed:%Y-%m-%d %H:%M}Z  |  {why}  |  {head[:90]}" for cid, changed, why, head in rows]
    lines += ["", f"ORPHANED IMAGES — card no longer exists ({len(orphans)}); deleted at the end of APPLY=1"
              + ("" if ok else f" — BLOCKED: {why_not}"), ""]
    lines += [f"{cid}  ({'card png' if cid in cards else ''}{' + ' if cid in cards and cid in graphs else ''}"
              f"{'graph poster' if cid in graphs else ''})" for cid in orphans]
    (out / "render_stale.txt").write_text("\n".join(lines) + "\n")
    (out / "render_stale.ids").write_text(",".join(r[0] for r in rows) + "\n")
    (out / "render_orphans.ids").write_text((",".join(orphans) if ok else "") + "\n")
    print(f"{len(rows)} stale of {len(docs)} cards, {len(orphans)} orphaned image ids → data/render_stale.txt",
          file=sys.stderr)
    print(os.path.relpath(out / "render_stale.ids", _ROOT))   # stdout: the ids file (repo-relative), for the make target
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
