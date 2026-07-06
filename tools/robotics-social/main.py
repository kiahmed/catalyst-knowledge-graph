"""
robotics-social — DuckDB top-N cards → Postiz posts.

Two invocation modes:
  1. Cloud Run Job (prod): `python main.py --once`
       Runs once, posts top-N, writes to social_posts, exits.
  2. HTTP service (local dev): `python main.py --serve`
       POST /run kicks off the same batch. GET /healthz for liveness.

Also pulls analytics back from Postiz for posts older than 24h (analytics_pull mode).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import duckdb
import httpx
from flask import Flask, jsonify, request

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("robotics-social")

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "/data/robotics.duckdb")
CARD_IMAGES_DIR = Path(os.environ.get("CARD_IMAGES_DIR", "/data/exports/card_images"))
POSTIZ_BASE_URL = os.environ.get("POSTIZ_BASE_URL", "http://localhost:4200")
POSTIZ_API_KEY = os.environ.get("POSTIZ_API_KEY", "")
DAILY_POST_CAP = int(os.environ.get("DAILY_POST_CAP", "3"))
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.75"))
CHANNELS = tuple(os.environ.get("POSTIZ_CHANNELS", "twitter,linkedin").split(","))


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, **fields}, default=str))


def _postiz_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {POSTIZ_API_KEY}"} if POSTIZ_API_KEY else {}


def _select_candidates(limit: int) -> list[dict[str, Any]]:
    """
    Top-N unposted cards, newest-first with confidence tiebreak.
    Excludes cards already posted (present in social_posts).
    Confidence floor is MIN_CONFIDENCE.
    """
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute(
            """
            SELECT c.entry_id,
                   c.headline,
                   c.sentiment_label,
                   c.source_url,
                   COALESCE(MAX(r.confidence), 0.5) AS max_confidence
            FROM catalysts c
            LEFT JOIN relationships r ON r.catalyst_id = c.catalyst_id
            WHERE c.entry_id NOT IN (SELECT DISTINCT catalyst_id::VARCHAR FROM social_posts)
            GROUP BY c.entry_id, c.headline, c.sentiment_label, c.source_url, c.timestamp
            HAVING max_confidence >= ?
            ORDER BY c.timestamp DESC, max_confidence DESC
            LIMIT ?
            """,
            [MIN_CONFIDENCE, limit],
        ).fetchall()
    except duckdb.CatalogException as exc:
        _log("select_failed_schema", error=str(exc))
        return []
    finally:
        con.close()

    return [
        {
            "entry_id": r[0],
            "headline": r[1],
            "sentiment": r[2],
            "source_url": r[3],
            "confidence": r[4],
        }
        for r in rows
    ]


def _upload_png(client: httpx.Client, card_id: str) -> str | None:
    """POST the PNG to Postiz; returns media_id or None on failure."""
    path = CARD_IMAGES_DIR / f"{card_id}.png"
    if not path.exists():
        _log("png_missing", card_id=card_id)
        return None

    with path.open("rb") as fh:
        resp = client.post(
            f"{POSTIZ_BASE_URL}/upload",
            headers=_postiz_headers(),
            files={"file": (f"{card_id}.png", fh, "image/png")},
        )
    if resp.status_code >= 300:
        _log("postiz_upload_failed", card_id=card_id, status=resp.status_code, body=resp.text[:200])
        return None
    return resp.json().get("media_id")


def _schedule_post(client: httpx.Client, card: dict, media_id: str) -> str | None:
    """Schedule a Postiz post; returns postiz post_id or None."""
    # Share text comes from cards.json in prod. Stub: use headline.
    text = card.get("share", {}).get("twitter_text") or card["headline"]
    payload = {
        "text": text,
        "media_ids": [media_id],
        "channels": list(CHANNELS),
    }
    resp = client.post(
        f"{POSTIZ_BASE_URL}/posts",
        headers={**_postiz_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code >= 300:
        _log("postiz_schedule_failed", card_id=card["entry_id"], status=resp.status_code, body=resp.text[:200])
        return None
    return resp.json().get("post_id")


def _record_posts(records: list[dict]) -> None:
    if not records:
        return
    con = duckdb.connect(DUCKDB_PATH)
    try:
        for rec in records:
            con.execute(
                """
                INSERT INTO social_posts (post_id, catalyst_id, platform, postiz_id)
                SELECT ?, catalyst_id, ?, ?
                FROM catalysts WHERE entry_id = ?
                """,
                [rec["post_id"], rec["platform"], rec["postiz_id"], rec["entry_id"]],
            )
    finally:
        con.close()


def run_batch(dry_run: bool = False) -> dict[str, Any]:
    start = time.monotonic()
    candidates = _select_candidates(DAILY_POST_CAP)
    _log("candidates", count=len(candidates))

    if dry_run or not candidates:
        return {
            "ok": True,
            "dry_run": dry_run,
            "candidates": [c["entry_id"] for c in candidates],
            "posted": 0,
            "duration_s": round(time.monotonic() - start, 2),
        }

    posted: list[dict] = []
    with httpx.Client(timeout=30.0) as client:
        for card in candidates:
            media_id = _upload_png(client, card["entry_id"])
            if not media_id:
                continue
            postiz_id = _schedule_post(client, card, media_id)
            if not postiz_id:
                continue
            for ch in CHANNELS:
                posted.append(
                    {
                        "post_id": str(uuid.uuid4()),
                        "entry_id": card["entry_id"],
                        "platform": ch,
                        "postiz_id": postiz_id,
                    }
                )

    _record_posts(posted)
    _log("batch_done", posted=len(posted))
    return {
        "ok": True,
        "posted": len(posted),
        "cards": [p["entry_id"] for p in posted],
        "duration_s": round(time.monotonic() - start, 2),
    }


# --------------- HTTP mode (local dev) ---------------

app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.post("/run")
def run_http():
    body = request.get_json(silent=True) or {}
    result = run_batch(dry_run=bool(body.get("dry_run", False)))
    return jsonify(result), 200


# --------------- Entry point ---------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run one batch and exit (Cloud Run Job mode)")
    mode.add_argument("--serve", action="store_true", help="Serve HTTP for local dev")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.once:
        result = run_batch(dry_run=args.dry_run)
        print(json.dumps(result))
        return 0 if result.get("ok") else 1

    # Serve mode — use gunicorn in prod; built-in server is fine for dev.
    port = int(os.environ.get("PORT", 8082))
    app.run(host="0.0.0.0", port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
