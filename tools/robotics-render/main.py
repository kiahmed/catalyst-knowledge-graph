"""
robotics-render — HTML card → PNG via Playwright.

Local: docker-compose up → POST http://localhost:8081/render
Prod : Cloud Run service, invoked after robotics-ingest success.

Endpoints:
    POST /render          → render one card PNG
        { "card_id": "ROB-041726-001" }
    POST /render-batch    → render all recent cards missing PNGs
        { "since_days": 7, "force": false }
    GET  /card/<id>       → OG deep-link page for social crawlers
    GET  /card-img/<id>.png → stream the card PNG from Firebase Storage
    GET  /healthz         → 200 OK

Single worker, Chromium launched once per request (Cloud Run concurrency=1).

The same image also runs as the `robotics-og` Cloud Run service with
OG_ONLY=true: only the GET routes serve; /render and /render-batch 404.
duckdb / jinja2 / playwright are imported lazily inside the render path so
the OG service (and tests) never need Chromium or the DuckDB file.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, request

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("robotics-render")

app = Flask(__name__)

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "/data/robotics.duckdb")
CARD_IMAGES_DIR = Path(os.environ.get("CARD_IMAGES_DIR", "/data/exports/card_images"))
# Prod: the DuckDB file lives in GCS; render copies it down before each
# request (read-only — render never writes the DB back). Unset locally,
# where DUCKDB_PATH is a bind-mounted file.
DUCKDB_GCS_BUCKET = os.environ.get("DUCKDB_GCS_BUCKET", "").strip()
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", "/templates"))
VIEWPORT_W = int(os.environ.get("VIEWPORT_WIDTH", "1200"))
VIEWPORT_H = int(os.environ.get("VIEWPORT_HEIGHT", "630"))

# Firebase Storage upload — gated by env, off in local dev by default.
def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")

STORAGE_UPLOAD_ENABLED = _env_bool("STORAGE_UPLOAD_ENABLED", False)
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "")
STORAGE_CARDS_PREFIX = os.environ.get("STORAGE_CARDS_PREFIX", "cards")

# OG deep-link mode — the robotics-og Cloud Run service sets OG_ONLY=true so
# the shared image serves only the GET routes (render endpoints 404).
OG_ONLY = _env_bool("OG_ONLY", False)
SECTOR = os.environ.get("SECTOR", "Robotics")
CANONICAL_ORIGIN = os.environ.get("CANONICAL_ORIGIN", "https://robotics.arboryx.ai").rstrip("/")

# Fallback tags when the Firestore doc is unavailable — mirror frontend/index.html.
SITE_NAME = "Arboryx · Robotics"
GENERIC_TITLE = "Robotics module · Arboryx"
GENERIC_DESCRIPTION = (
    "Embodied AI catalysts — daily entity-graph view of who's building "
    "what across the robotics ecosystem."
)

_CARD_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, **fields}, default=str))


_storage_client_singleton = None


def _cards_bucket():
    """Lazy-init the cards-bucket handle for the READ path (OG routes).
    Needs only STORAGE_BUCKET — the upload gate doesn't apply to reads.
    Returns None when the bucket is unset."""
    global _storage_client_singleton
    if not STORAGE_BUCKET:
        return None
    if _storage_client_singleton is None:
        from google.cloud import storage
        _storage_client_singleton = storage.Client()
    return _storage_client_singleton.bucket(STORAGE_BUCKET)


def _storage_bucket():
    """Bucket handle for the UPLOAD path. Returns None when upload disabled
    or bucket unset (so the caller can branch cleanly)."""
    if not STORAGE_UPLOAD_ENABLED:
        return None
    return _cards_bucket()


def _upload_png(card_id: str, local_path: Path) -> dict[str, Any]:
    """Upload a single PNG to Firebase Storage. No-op when upload disabled.

    Idempotent — skips when the blob already exists. Returns a small dict
    summarizing what happened.
    """
    bucket = _storage_bucket()
    if bucket is None:
        return {"uploaded": False, "reason": "upload_disabled"}
    blob = bucket.blob(f"{STORAGE_CARDS_PREFIX}/{card_id}.png")
    if blob.exists():
        return {"uploaded": False, "reason": "already_exists"}
    # Private bucket — no make_public(). Browsers read via the Firebase
    # Storage SDK gated by storage.rules; this SA write is IAM-governed.
    blob.upload_from_filename(str(local_path), content_type="image/png")
    return {"uploaded": True, "blob": blob.name}


def _pull_duckdb() -> dict[str, Any]:
    """Copy the DuckDB file down from GCS before rendering. No-op locally.

    Render opens DuckDB read-only and never writes it back, so this is a
    one-way pull. Mirrors src/duckdb_sync.pull(); kept inline because the
    render image is deliberately standalone (no src/ package).
    """
    if not DUCKDB_GCS_BUCKET:
        return {"pulled": False, "reason": "local_mode"}
    from google.cloud import storage

    obj = os.path.basename(DUCKDB_PATH)
    blob = storage.Client().bucket(DUCKDB_GCS_BUCKET).blob(obj)
    if not blob.exists():
        return {"pulled": False, "reason": "no_remote_object"}
    Path(DUCKDB_PATH).parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(DUCKDB_PATH)
    return {"pulled": True, "object": f"gs://{DUCKDB_GCS_BUCKET}/{obj}"}


# rel_type → (group used for CSS color, human-readable label)
_REL_BUCKETS: dict[str, tuple[str, str]] = {
    "partners_with":       ("partnership", "Partnership"),
    "integrates_with":     ("partnership", "Integration"),
    "pilots":              ("partnership", "Pilot"),
    "deploys":             ("partnership", "Deployment"),
    "acquires":            ("deal",        "Acquisition"),
    "invests_in":          ("deal",        "Investment"),
    "supplies":            ("deal",        "Supply Agreement"),
    "competes_with":       ("competitive", "Competition"),
    "displaces":           ("competitive", "Displacement"),
    "benchmarks_against":  ("competitive", "Benchmark"),
    "regulates":           ("regulatory",  "Regulation"),
    "litigates_against":   ("regulatory",  "Litigation"),
    "hires_from":          ("talent",      "Talent"),
    "spins_out_from":      ("talent",      "Spinout"),
    "built_on":            ("technical",   "Technical"),
}


def _subtitle_from_takeaways(takeaways: str | None) -> str:
    """First 'Direct:' / 'Market Dynamics:' line, trimmed to ~140 chars."""
    if not takeaways:
        return ""
    for line in takeaways.splitlines():
        line = line.strip()
        if line.lower().startswith(("direct:", "market dynamics:", "indirect:")):
            line = line.split(":", 1)[-1].strip()
            return line[:140] + ("…" if len(line) > 140 else "")
    first = takeaways.strip().splitlines()[0].strip()
    return first[:140] + ("…" if len(first) > 140 else "")


def _load_card(card_id: str) -> dict[str, Any] | None:
    """Build the rich payload the template expects (matches src/export.py
    `_load_cards` shape, narrowed to one entry)."""
    import duckdb

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        row = con.execute(
            """
            SELECT entry_id, sector, timestamp, headline, sentiment_takeaways,
                   sentiment_label, source_url
            FROM catalysts
            WHERE entry_id = ?
            """,
            [card_id],
        ).fetchone()
        if not row:
            return None
        (entry_id, sector, ts, headline, takeaways, sentiment_label, source_url) = row
        catalyst_id_row = con.execute(
            "SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [card_id]
        ).fetchone()
        catalyst_id = catalyst_id_row[0] if catalyst_id_row else None

        rels: list[dict[str, Any]] = []
        if catalyst_id is not None:
            rel_rows = con.execute(
                """
                SELECT ea.name, ea.ticker, eb.name, eb.ticker, r.rel_type, r.confidence
                FROM relationships r
                JOIN entities ea ON ea.entity_id = r.entity_a_id
                JOIN entities eb ON eb.entity_id = r.entity_b_id
                WHERE r.catalyst_id = ? AND r.status IN ('active', 'materialized')
                ORDER BY r.confidence DESC
                """,
                [catalyst_id],
            ).fetchall()
            rels = [
                {"a": {"name": an, "ticker": at}, "b": {"name": bn, "ticker": bt},
                 "rel_type": rt, "confidence": float(c)}
                for (an, at, bn, bt, rt, c) in rel_rows
            ]
    finally:
        con.close()

    # Pick top rel for the entity pair shown on the card.
    top = rels[0] if rels else None
    if top:
        entity_a, entity_b = top["a"], top["b"]
        confidence = round(top["confidence"], 2)
        rel_group, rel_type_label = _REL_BUCKETS.get(
            top["rel_type"], ("catalyst", top["rel_type"].replace("_", " ").title()),
        )
    else:
        entity_a = entity_b = None
        confidence = None
        rel_group, rel_type_label = "catalyst", "Catalyst"

    # "Also mentioned" — entity names from the remaining rels, deduped, capped.
    seen = {entity_a["name"] if entity_a else None, entity_b["name"] if entity_b else None}
    related: list[str] = []
    for r in rels[1:]:
        for end in (r["a"], r["b"]):
            if end["name"] and end["name"] not in seen:
                related.append(end["name"])
                seen.add(end["name"])
        if len(related) >= 4:
            break

    return {
        "card_type": "catalyst",                 # canonical branch in card.html
        "sector": sector,
        "sector_key": (sector or "robotics").lower().replace(" ", "_"),
        "rel_type_label": rel_type_label,
        "rel_group": rel_group,
        "date": ts.isoformat() if ts else None,
        "headline": headline,
        "subtitle": _subtitle_from_takeaways(takeaways),
        "entity_a": entity_a,
        "entity_b": entity_b,
        "confidence": confidence,
        "related": related,
        "velocity": None,                        # populated by detectors (Phase 1 W4)
    }


def _render_png(card: dict[str, Any]) -> bytes:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from playwright.sync_api import sync_playwright

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(),
    )
    template = env.get_template("card.html")
    html = template.render(**card)

    # Inline base.css so set_content() doesn't try to fetch it over the network
    # (template uses `<link rel="stylesheet" href="base.css">`, which won't
    # resolve when the page is set via set_content with no base URL).
    css_path = TEMPLATES_DIR / "base.css"
    if css_path.exists():
        css = css_path.read_text()
        html = html.replace(
            '<link rel="stylesheet" href="base.css">',
            f"<style>{css}</style>",
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            # Social platforms crop previews to ~1.91:1 (1200x630). The card's
            # natural layout is narrow/portrait (~480px), so neither an
            # element screenshot (head gets cropped) nor shrink-to-fit (card
            # floats small in margins, blurry once platforms downscale) reads
            # well. Instead: relayout the card WIDE so it fills the frame —
            # width pinned near the canvas, content reflows, then a final
            # scale-to-fit guard for unusually tall cards. device_scale_factor
            # 2 renders at 2400x1260 for a crisp downscale on the platforms.
            page = browser.new_page(
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                device_scale_factor=2,
            )
            page.set_content(html, wait_until="networkidle")
            page.evaluate(
                """([w, h]) => {
                  const card = document.querySelector('.sp-card');
                  document.documentElement.style.cssText +=
                    `;width:${w}px;height:${h}px;overflow:hidden;`;
                  document.body.style.cssText +=
                    `;width:${w}px;height:${h}px;margin:0;padding:0;` +
                    'display:flex;align-items:center;justify-content:center;overflow:hidden;';
                  // Share previews display ~550px wide in the feed, so the
                  // image must read like a poster, not a webpage: drop the
                  // secondary sections and lay out narrow (640px), then scale
                  // up ~1.8x to fill the canvas — headline/subtitle land at
                  // roughly 2x their site size, readable even after the
                  // platforms downscale and recompress.
                  for (const sel of ['.sp-related', '.sp-card-footer']) {
                    const el = card.querySelector(sel);
                    if (el) el.style.display = 'none';
                  }
                  card.style.width = '640px';
                  card.style.maxWidth = 'none';
                  card.style.margin = '0';
                  const r = card.getBoundingClientRect();
                  const s = Math.min((w * 0.97) / r.width, (h * 0.97) / r.height);
                  card.style.transform = `scale(${s})`;
                  card.style.transformOrigin = 'center center';
                }""",
                [VIEWPORT_W, VIEWPORT_H],
            )
            png = page.screenshot(
                clip={"x": 0, "y": 0, "width": VIEWPORT_W, "height": VIEWPORT_H}
            )
        finally:
            browser.close()
    return png


@app.get("/healthz")
def healthz():
    return "ok", 200


# ── OG deep links (served by the robotics-og service) ──────────────
# Social crawlers don't run JS, so /card/<id> returns per-card OG tags
# pointing at /card-img/<id>.png (the PNG robotics-render already uploaded
# to the private cards bucket). Humans get bounced to /?card=<id>.
# These routes never touch DuckDB or Playwright.

_firestore_client_singleton = None
_card_doc_cache: dict[str, dict[str, Any] | None] = {}


def _fetch_card_doc(card_id: str) -> dict[str, Any] | None:
    """Card doc from Firestore CKG-<SECTOR>/catalysts/items/<card_id>.
    Lookups (hits AND misses) are cached in-memory for the instance's
    lifetime — card docs are effectively immutable once exported."""
    if card_id in _card_doc_cache:
        return _card_doc_cache[card_id]
    global _firestore_client_singleton
    if _firestore_client_singleton is None:
        from google.cloud import firestore
        _firestore_client_singleton = firestore.Client()
    snap = (
        _firestore_client_singleton.collection(f"CKG-{SECTOR}")
        .document("catalysts")
        .collection("items")
        .document(card_id)
        .get()
    )
    doc = snap.to_dict() if snap.exists else None
    _card_doc_cache[card_id] = doc
    return doc


def _png_blob_generation(card_id: str) -> int | None:
    """GCS generation of the card PNG, or None if missing/unreachable.

    The generation changes on every overwrite, so baking it into the
    og:image URL busts both the Firebase CDN and the platforms' media
    caches (X/LinkedIn cache the image by URL) whenever a card is
    re-rendered."""
    bucket = _cards_bucket()
    if bucket is None:
        return None
    try:
        blob = bucket.get_blob(f"{STORAGE_CARDS_PREFIX}/{card_id}.png")
        return blob.generation if blob is not None else None
    except Exception as exc:
        _log("og_blob_check_failed", card_id=card_id, error=str(exc))
        return None


def _og_page(card_id: str, title: str, description: str,
             generation: int | None = None) -> str:
    """OG/Twitter tag page. Every injected value is HTML-escaped; card_id is
    already validated against _CARD_ID_RE by the route."""
    esc = html_mod.escape
    image_url = f"{CANONICAL_ORIGIN}/card-img/{card_id}.png"
    if generation:
        image_url += f"?g={generation}"
    page_url = f"{CANONICAL_ORIGIN}/card/{card_id}"
    redirect_path = f"/?card={card_id}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{esc(title)}</title>
<meta property="og:type" content="article">
<meta property="og:site_name" content="{esc(SITE_NAME)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:image" content="{esc(image_url)}">
<meta property="og:image:width" content="2400">
<meta property="og:image:height" content="1260">
<meta property="og:image:type" content="image/png">
<meta property="og:url" content="{esc(page_url)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(description)}">
<meta name="twitter:image" content="{esc(image_url)}">
<meta http-equiv="refresh" content="0;url={esc(redirect_path)}">
</head>
<body>
<script>location.replace({json.dumps(redirect_path)});</script>
<p><a href="{esc(redirect_path)}">View this catalyst card</a></p>
</body>
</html>"""


@app.get("/card/<card_id>")
def og_card_page(card_id: str):
    if not _CARD_ID_RE.match(card_id):
        return "not found", 404

    doc = None
    try:
        doc = _fetch_card_doc(card_id)
    except Exception as exc:
        _log("og_firestore_failed", card_id=card_id, error=str(exc))

    generation = _png_blob_generation(card_id)

    if doc is not None:
        title = doc.get("headline") or GENERIC_TITLE
        description = doc.get("subtitle") or doc.get("summary") or GENERIC_DESCRIPTION
    elif generation is not None:
        # No doc but the PNG exists — still give crawlers the image with
        # generic text rather than a dead preview.
        title, description = GENERIC_TITLE, GENERIC_DESCRIPTION
    else:
        return redirect(CANONICAL_ORIGIN, code=302)

    return (
        _og_page(card_id, title, description, generation),
        200,
        {"Content-Type": "text/html; charset=utf-8",
         "Cache-Control": "public, max-age=300"},
    )


@app.get("/card-img/<card_id>.png")
def og_card_image(card_id: str):
    if not _CARD_ID_RE.match(card_id):
        return "not found", 404
    bucket = _cards_bucket()
    if bucket is None:
        return "not found", 404
    blob = bucket.blob(f"{STORAGE_CARDS_PREFIX}/{card_id}.png")
    if not blob.exists():
        return "not found", 404
    # Card PNGs are ~100-300KB — download_as_bytes is fine at these sizes.
    # max-age kept short: blobs get re-rendered/overwritten (e.g. the
    # 1200x630 re-frame), and a long-lived Firebase CDN copy strands
    # social crawlers on the stale image for the full TTL.
    return (
        blob.download_as_bytes(),
        200,
        {"Content-Type": "image/png",
         "Cache-Control": "public, max-age=3600, must-revalidate"},
    )


@app.post("/render")
def render_one():
    if OG_ONLY:
        return jsonify({"ok": False, "error": "not found"}), 404
    start = time.monotonic()
    body = request.get_json(silent=True) or {}
    card_id = body.get("card_id")
    if not card_id:
        return jsonify({"ok": False, "error": "card_id is required"}), 400

    _log("duckdb_pull", **_pull_duckdb())
    card = _load_card(card_id)
    if not card:
        return jsonify({"ok": False, "error": f"card not found: {card_id}"}), 404

    CARD_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CARD_IMAGES_DIR / f"{card_id}.png"

    png = _render_png(card)
    out_path.write_bytes(png)

    upload = _upload_png(card_id, out_path)
    duration = round(time.monotonic() - start, 2)
    _log("render_ok", card_id=card_id, bytes=len(png), duration_s=duration, upload=upload)
    return jsonify({
        "ok": True, "card_id": card_id, "path": str(out_path),
        "duration_s": duration, "upload": upload,
    }), 200


def _parse_batch_body(body: dict) -> dict:
    """Unwrap a Pub/Sub push envelope into render-batch params.

    Push delivery wraps the payload: {"message": {"data": "<base64 JSON>"}, ...}.
    Plain JSON bodies pass through unchanged. An envelope whose data can't be
    decoded to a JSON object is treated as an empty body — mode 1 (full
    backlog, exists-skip), which is the safe default for the ingest-done event.
    """
    if not isinstance(body, dict):
        return {}
    message = body.get("message")
    if not isinstance(message, dict):
        return body
    data = message.get("data")
    if not data:
        return {}
    try:
        decoded = json.loads(base64.b64decode(data).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


@app.post("/render-batch")
def render_batch():
    """Render PNGs for catalysts. Three modes:

      1. No args:       render every unrendered catalyst (full backlog)
      2. limit=N:       render up to N latest catalysts
      3. from_id=X +    render up to N catalysts starting at entry_id X
         limit=N        (oldest-forward from that anchor)

    All modes skip catalysts whose PNG already exists (unless force=true).
    """
    if OG_ONLY:
        return jsonify({"ok": False, "error": "not found"}), 404
    start = time.monotonic()
    body = _parse_batch_body(request.get_json(silent=True) or {})
    force = bool(body.get("force", False))
    limit = body.get("limit")
    limit = int(limit) if limit not in (None, "", 0, "0") else None
    from_id = (body.get("from_id") or "").strip() or None

    _log("duckdb_pull", **_pull_duckdb())

    if from_id:
        # Mode 3: from a specific entry_id, oldest-forward. DuckDB can't
        # compare a row tuple against a multi-column subquery directly —
        # join the anchor row and compare fields instead.
        sql = """
            SELECT c.entry_id FROM catalysts c,
                 (SELECT timestamp AS ats, entry_id AS aid
                  FROM catalysts WHERE entry_id = ?) a
            WHERE c.timestamp > a.ats
               OR (c.timestamp = a.ats AND c.entry_id >= a.aid)
            ORDER BY c.timestamp ASC, c.entry_id ASC
        """
        params: list[Any] = [from_id]
    else:
        # Modes 1 + 2: newest-first
        sql = "SELECT entry_id FROM catalysts ORDER BY timestamp DESC, entry_id DESC"
        params = []
    if limit:
        sql += f" LIMIT {limit}"

    import duckdb

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    CARD_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    rendered, skipped, failed, uploaded, upload_skipped = 0, 0, 0, 0, 0
    for (entry_id,) in rows:
        out_path = CARD_IMAGES_DIR / f"{entry_id}.png"
        if out_path.exists() and not force:
            skipped += 1
        else:
            card = _load_card(entry_id)
            if not card:
                failed += 1
                continue
            try:
                out_path.write_bytes(_render_png(card))
                rendered += 1
            except Exception as exc:
                failed += 1
                _log("render_failed", card_id=entry_id, error=str(exc))
                continue

        # Upload regardless of whether we just rendered or skipped — covers
        # the case where local PNGs exist but Storage doesn't (first cutover).
        try:
            up = _upload_png(entry_id, out_path)
            if up.get("uploaded"):
                uploaded += 1
            else:
                upload_skipped += 1
        except Exception as exc:
            _log("upload_failed", card_id=entry_id, error=str(exc))

    duration = round(time.monotonic() - start, 2)
    _log("batch_ok", total=len(rows), rendered=rendered, skipped=skipped,
         failed=failed, uploaded=uploaded, upload_skipped=upload_skipped,
         duration_s=duration)
    return jsonify({
        "ok": True, "total": len(rows), "rendered": rendered, "skipped": skipped,
        "failed": failed, "uploaded": uploaded, "upload_skipped": upload_skipped,
        "duration_s": duration,
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8081)))
