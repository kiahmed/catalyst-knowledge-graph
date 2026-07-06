#!/usr/bin/env python3
"""reframe_cards.py — re-render old-format card PNGs to the 2400x1260 poster frame.

The 2026-07-05 render rework (900px→640px poster layout, 2x density)
made every pre-existing PNG stale: old files are the card's natural size
(~480x426) or an intermediate frame — social platforms center-crop those.
This tool finds every PNG that is NOT the current 2400x1260 format and
re-renders it through the local robotics-render container.

Idempotent / resumable: the PNG's own header is the state. A file that
is already 2400x1260 is skipped, so an interrupted run just continues
where it left off on the next invocation. No state file.

Usage (stack must be up: `make up`):
    python3 dev-utils/reframe_cards.py --dry-run          # classify only
    python3 dev-utils/reframe_cards.py --limit 1          # test with one
    python3 dev-utils/reframe_cards.py                    # convert the rest
    python3 dev-utils/reframe_cards.py --dir /other/path  # non-default dir
    python3 dev-utils/reframe_cards.py --card ROB-051326-003

Batching: renders sequentially (the render container is concurrency=1 /
one Chromium per request) in batches of --batch (default 5), with a
health probe between batches — a wedged container fails fast instead of
hanging the whole run. Per-card failures are collected and reported; the
run continues. Exit 0 = everything converted; 1 = some cards failed;
2 = usage/environment error.

NOTE: local disk only. The bucket upload path skips existing blobs, so
re-uploading corrected PNGs to gs://robotics-cards is a separate step
(blob delete + re-upload, or firestore-sync won't touch them either).
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = REPO_ROOT / "data" / "exports" / "card_images"
RENDER_PORT = os.environ.get("RENDER_PORT", "8081")
RENDER_URL = f"http://localhost:{RENDER_PORT}"
GOOD_DIMS = (2400, 1260)  # 1200x630 canvas at device_scale_factor=2
PER_CARD_TIMEOUT = 120    # Chromium render is ~5-15s; hard cap per card
BATCH_PAUSE = 2           # seconds between batches — let the worker breathe


def png_dims(path: Path) -> tuple[int, int] | None:
    """Width/height from the PNG IHDR — no image library needed.
    Returns None for unreadable/non-PNG files."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", head[16:24])
    except OSError:
        return None


def classify(directory: Path) -> tuple[list[str], list[str], list[str]]:
    """→ (needs_reframe, already_good, unreadable) card_ids sorted."""
    needs, good, bad = [], [], []
    for p in sorted(directory.glob("*.png")):
        dims = png_dims(p)
        if dims is None:
            bad.append(p.stem)
        elif dims == GOOD_DIMS:
            good.append(p.stem)
        else:
            needs.append(p.stem)
    return needs, good, bad


def render_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{RENDER_URL}/healthz", timeout=10) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def render_one(card_id: str) -> tuple[bool, str]:
    """POST /render for one card. → (ok, detail)."""
    req = urllib.request.Request(
        f"{RENDER_URL}/render",
        data=json.dumps({"card_id": card_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PER_CARD_TIMEOUT) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"request failed: {e}"
    except json.JSONDecodeError as e:
        return False, f"bad response JSON: {e}"
    if not body.get("ok"):
        return False, f"render error: {body.get('error', body)}"
    return True, f"{body.get('duration_s', '?')}s"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                    help=f"card_images directory (default: {DEFAULT_DIR})")
    ap.add_argument("--limit", type=int, default=0,
                    help="convert at most N cards this run (0 = all)")
    ap.add_argument("--batch", type=int, default=5,
                    help="cards per batch between health probes (default 5)")
    ap.add_argument("--card", help="convert exactly this card_id and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and list; render nothing")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"!! not a directory: {args.dir}")
        return 2

    needs, good, bad = classify(args.dir)
    if args.card:
        if args.card in good:
            print(f"{args.card}: already {GOOD_DIMS[0]}x{GOOD_DIMS[1]} — nothing to do")
            return 0
        needs = [args.card]

    print(f"{args.dir}: {len(good)} already good, {len(needs)} need reframe"
          + (f", {len(bad)} unreadable ({', '.join(bad)})" if bad else ""))
    if args.dry_run or not needs:
        for cid in needs:
            print(f"  needs: {cid} {png_dims(args.dir / (cid + '.png'))}")
        return 0

    if args.limit > 0:
        needs = needs[: args.limit]

    if not render_healthy():
        print(f"!! render service not responding at {RENDER_URL}/healthz — is the stack up? (make up)")
        return 2

    failed: list[tuple[str, str]] = []
    converted = 0
    for i, cid in enumerate(needs):
        if i and i % args.batch == 0:
            time.sleep(BATCH_PAUSE)
            if not render_healthy():
                print(f"!! render service went unhealthy after {i} cards — stopping. "
                      "Re-run to resume (already-converted files are skipped).")
                break
        ok, detail = render_one(cid)
        if ok:
            dims = png_dims(args.dir / f"{cid}.png")
            if dims == GOOD_DIMS:
                converted += 1
                print(f"  ok   {cid} ({detail})")
            else:
                failed.append((cid, f"rendered but dims are {dims}, expected {GOOD_DIMS}"))
                print(f"  BAD  {cid} — rendered but dims {dims}")
        else:
            failed.append((cid, detail))
            print(f"  FAIL {cid} — {detail}")

    remaining = len(classify(args.dir)[0])
    print(f"\nconverted {converted}, failed {len(failed)}, still needing reframe: {remaining}")
    if failed:
        print("failures (fix cause, then just re-run — resume is automatic):")
        for cid, why in failed:
            print(f"  {cid}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
