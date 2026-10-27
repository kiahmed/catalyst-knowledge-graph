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
    GET  /graph-img/<id>.png → stream the catalyst subgraph PNG
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
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
# Catalyst subgraph posters live beside the cards under their own prefix.
STORAGE_GRAPHS_PREFIX = os.environ.get("STORAGE_GRAPHS_PREFIX", "graphs")
GRAPH_IMAGES_DIR = Path(os.environ.get("GRAPH_IMAGES_DIR", "/data/exports/graph_images"))
# Weekly recap chart cards (spec §2.9b): recaps/<sector>/<YYYY-Www>/<card>.png —
# week-keyed and WRITE-ONCE (unlike graphs/, which force runs overwrite), so
# consumers can cache on the URL.
STORAGE_RECAPS_PREFIX = os.environ.get("STORAGE_RECAPS_PREFIX", "recaps")
RECAP_IMAGES_DIR = Path(os.environ.get("RECAP_IMAGES_DIR", "/data/exports/recap_images"))
# Node/edge/font sizes in the site's mini-graph are tuned for a ~240px panel;
# the poster canvas is ~5x that, so scale them up to keep the same look.
GRAPH_SCALE = float(os.environ.get("GRAPH_SCALE", "1.7"))

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

_CARD_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}\Z")


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


def _bucket_png_ids() -> set[str]:
    """card_ids that already have a PNG in Storage. Empty set when the
    bucket is unset (local dev without storage) — local-disk check rules."""
    if not STORAGE_BUCKET:
        return set()
    bucket = _cards_bucket()
    prefix = f"{STORAGE_CARDS_PREFIX}/"
    return {
        blob.name[len(prefix):-len(".png")]
        for blob in bucket.list_blobs(prefix=prefix)
        if blob.name.endswith(".png")
    }


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

    # Social platforms crop previews to ~1.91:1 (1200x630). The card's
    # natural layout is narrow/portrait (~480px), so neither an
    # element screenshot (head gets cropped) nor shrink-to-fit (card
    # floats small in margins, blurry once platforms downscale) reads
    # well. Instead: relayout the card WIDE so it fills the frame —
    # width pinned near the canvas, content reflows, then a final
    # scale-to-fit guard for unusually tall cards. device_scale_factor
    # 2 renders at 2400x1260 for a crisp downscale on the platforms.
    # Same per-thread browser as the graph posters: a second sync_playwright()
    # on a thread that already runs one fails ("Sync API inside the asyncio
    # loop"), which dropped every card render after a poster in a batch.
    page = _shared_browser().new_page(
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        device_scale_factor=2,
    )
    try:
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
        page.close()
    return png


# ── Weekly recap chart cards (spec §2.9b) ──────────────────────────

def _recap_week_doc(sector: str) -> dict[str, Any] | None:
    """This week's ARCHIVED recap (CKG-<sector>/recaps/weeks/<week>), the
    write-once copy — not the graph doc's daily-refreshed one — so the images
    always match what the week doc says."""
    from google.cloud import firestore

    collection = os.environ.get("FIRESTORE_EXPORT_COLLECTION", f"CKG-{sector}")
    db = firestore.Client()
    live = (db.collection(collection).document("graph").collection("sectors")
            .document(sector).get())
    week = ((live.to_dict() or {}).get("recap") or {}).get("week") if live.exists else None
    if not week:
        return None
    snap = (db.collection(collection).document("recaps").collection("weeks")
            .document(week).get())
    return snap.to_dict() if snap.exists else None


def _render_recap_png(card: dict[str, Any], recap: dict[str, Any], sector: str) -> bytes:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                      autoescape=select_autoescape())
    html = env.get_template("recap.html").render(
        week=recap["week"], sector=sector,
        width=VIEWPORT_W, height=VIEWPORT_H,
        # Objects, not pre-dumped JSON: the template embeds them with Jinja's
        # `tojson`, which escapes < > & ' — entity names come from LLM
        # extraction, so a "</script>" in one must not break out of the tag.
        card=card, groups=recap.get("groups") or [],
    )
    page = _shared_browser().new_page(
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}, device_scale_factor=2)
    try:
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__recapReady === true", timeout=15000)
        return page.screenshot(clip={"x": 0, "y": 0, "width": VIEWPORT_W, "height": VIEWPORT_H})
    finally:
        page.close()


def _render_recaps(sector: str, force: bool = False) -> dict[str, Any]:
    """Render + upload every card of this week's recap. Write-once per week:
    existing images are skipped (force=True re-renders and replaces)."""
    recap = _recap_week_doc(sector)
    if not recap or not recap.get("cards"):
        return {"recap_week": None, "recaps_rendered": 0}
    week = recap["week"]
    bucket = _storage_bucket()
    out_dir = RECAP_IMAGES_DIR / sector / week
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = skipped = failed = 0
    for card in recap["cards"]:
        name = f"{STORAGE_RECAPS_PREFIX}/{sector}/{week}/{card['id']}.png"
        try:
            if bucket is not None and not force and bucket.blob(name).exists():
                skipped += 1
                continue
            path = out_dir / f"{card['id']}.png"
            path.write_bytes(_render_recap_png(card, recap, sector))
            if bucket is not None:
                bucket.blob(name).upload_from_filename(str(path), content_type="image/png")
            rendered += 1
        except Exception as exc:
            failed += 1
            _log("recap_failed", card_id=card.get("id"), week=week, error=str(exc))
    return {"recap_week": week, "recaps_rendered": rendered,
            "recaps_skipped": skipped, "recaps_failed": failed}


# ── Catalyst subgraph posters ──────────────────────────────────────

_graph_doc_cache: dict[str, Any] = {}


def _graph_doc(sector: str) -> dict[str, Any] | None:
    """The same `graph` payload the website loads: CKG-<sector>/graph/sectors/
    <sector>. Read once per process — it is identical for every card in a
    batch and is ~1MB."""
    if sector in _graph_doc_cache:
        return _graph_doc_cache[sector]
    from google.cloud import firestore

    collection = os.environ.get("FIRESTORE_EXPORT_COLLECTION", f"CKG-{sector}")
    snap = (firestore.Client()
            .collection(collection).document("graph")
            .collection("sectors").document(sector).get())
    doc = (snap.to_dict() or {}).get("graph") if snap.exists else None
    _graph_doc_cache[sector] = doc
    return doc


def _bucket_graph_ids() -> set[str]:
    if not STORAGE_BUCKET:
        return set()
    prefix = f"{STORAGE_GRAPHS_PREFIX}/"
    return {b.name[len(prefix):-len(".png")]
            for b in _cards_bucket().list_blobs(prefix=prefix)
            if b.name.endswith(".png")}


def _upload_graph_png(card_id: str, local_path: Path,
                      overwrite: bool = False) -> dict[str, Any]:
    """overwrite=True (force runs) replaces a stale poster; default keeps the
    upload-once behaviour so routine sweeps never rewrite existing images."""
    bucket = _storage_bucket()
    if bucket is None:
        return {"uploaded": False, "reason": "upload_disabled"}
    blob = bucket.blob(f"{STORAGE_GRAPHS_PREFIX}/{card_id}.png")
    if not overwrite and blob.exists():
        return {"uploaded": False, "reason": "already_exists"}
    blob.upload_from_filename(str(local_path), content_type="image/png")
    return {"uploaded": True, "blob": blob.name}


def _card_entity_names(card_id: str) -> list[str]:
    """Entity names in this catalyst's relationships — the same set the site's
    cardEntityNames(card) derives from the card payload."""
    import duckdb

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute(
            """
            SELECT DISTINCT e.name
            FROM catalysts c
            JOIN relationships r ON r.catalyst_id = c.catalyst_id
            JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
            WHERE c.entry_id = ?
            ORDER BY e.name
            """,
            [card_id],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


_browser_tls = threading.local()


def _shared_browser():
    """One Chromium PER THREAD, reused across renders. Launching per render
    costs ~2-3s each, which turns a 500-card backfill into hours. Per-thread,
    not per-process: Playwright's sync API is bound to the thread (greenlet)
    that started it, and gunicorn runs --threads 2 — a process-wide singleton
    made every request on the other thread fail ("Cannot switch to a
    different thread"), silently failing whole render batches."""
    b = getattr(_browser_tls, "browser", None)
    if b is not None and b.is_connected():
        return b
    # Chromium died (OOM, crash): stop this thread's old Playwright before
    # starting another — a second live sync_playwright() on the thread raises
    # "Sync API inside the asyncio loop" and every later render would fail
    # until the instance recycled.
    old_pw = getattr(_browser_tls, "pw", None)
    if old_pw is not None:
        try:
            old_pw.stop()
        except Exception:
            pass
        _browser_tls.pw = _browser_tls.browser = None
    from playwright.sync_api import sync_playwright

    _browser_tls.pw = sync_playwright().start()
    _browser_tls.browser = _browser_tls.pw.chromium.launch(args=["--no-sandbox"])
    return _browser_tls.browser


def _with_card_edges(graph: dict[str, Any], card_id: str) -> dict[str, Any]:
    """Graph doc + this catalyst's own edges from DuckDB, when the doc lacks them.

    The graph doc is a size-capped recent window (src/export.py
    _fit_graph_budget), so older catalysts' edges aren't in it — and while
    the doc was frozen (2026-08-18 → 10-14) newer ones weren't either. The
    template then fell back to unrelated historical edges between the card's
    entities, drawing e.g. Google isolated. DuckDB is the source of truth;
    inject the card's edges (catalyst_id-tagged) so the template's primary
    `e.catalyst_id === CARD_ID` path always draws the right subgraph.
    """
    edges = graph.get("edges") or []
    if any(e.get("catalyst_id") == card_id for e in edges):
        return graph
    import duckdb

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute(
            """
            SELECT ea.name, ea.ticker, ea.type, eb.name, eb.ticker, eb.type,
                   r.rel_type, r.confidence, r.evidence_type, r.status,
                   r.mechanism, c.timestamp
            FROM catalysts c
            JOIN relationships r ON r.catalyst_id = c.catalyst_id
            JOIN entities ea ON ea.entity_id = r.entity_a_id
            JOIN entities eb ON eb.entity_id = r.entity_b_id
            WHERE c.entry_id = ? AND r.status IN ('active', 'materialized')
              AND r.entity_a_id <> r.entity_b_id
            """,
            [card_id],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return graph
    nodes = list(graph.get("nodes") or [])
    have = {n.get("id") for n in nodes}
    extra_edges = []
    for (a, at, aty, b, bt, bty, rel, conf, ev, status, mech, ts) in rows:
        for name, ticker, typ in ((a, at, aty), (b, bt, bty)):
            if name not in have:
                have.add(name)
                nodes.append({"id": name, "name": name, "ticker": ticker,
                              "type": typ, "heat": 0.0, "edge_count": 0})
        extra_edges.append({"from": a, "to": b, "rel": rel,
                            "confidence": float(conf or 0.0), "evidence_type": ev,
                            "status": status, "mechanism": mech,
                            "catalyst_id": card_id, "catalyst_date": str(ts)})
    return {**graph, "nodes": nodes, "edges": edges + extra_edges}


def _render_graph_png(card_id: str, card: dict[str, Any]) -> bytes:
    """Screenshot the catalyst's subgraph using the SAME cytoscape code and
    styles as the site's mini-graph (templates/cards/graph.html)."""
    import json as _json
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    sector = card.get("sector") or os.environ.get("SECTOR", "Robotics")
    graph = _graph_doc(sector)
    if not graph:
        raise RuntimeError(f"no graph doc for sector {sector}")
    graph = _with_card_edges(graph, card_id)

    names = _card_entity_names(card_id)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                      autoescape=select_autoescape())
    html = env.get_template("graph.html").render(
        card_id=card_id, headline=card.get("headline", ""),
        date=card.get("date", ""), sector=sector,
        width=VIEWPORT_W, height=VIEWPORT_H, scale=GRAPH_SCALE,
        graph_json=_json.dumps(graph), card_id_json=_json.dumps(card_id),
        entity_names_json=_json.dumps(names),
        min_confidence=float(os.environ.get("GRAPH_MIN_CONFIDENCE", "0")),
        show_invalidated=False,
    )
    # set_content() has no base URL, so relative <script src> would 404 —
    # inline the vendored cytoscape bundles instead.
    for rel in ("vendor/cytoscape.min.js", "vendor/layout-base.js",
                "vendor/cose-base.js", "vendor/cytoscape-cose-bilkent.js"):
        path = TEMPLATES_DIR / rel
        js = path.read_text() if path.exists() else ""
        html = html.replace(f'<script src="{rel}"></script>', f"<script>{js}</script>")

    page = _shared_browser().new_page(
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        device_scale_factor=2,
    )
    try:
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__graphReady !== undefined", timeout=30000)
        page.wait_for_timeout(250)   # let the layout settle before the shot
        return page.screenshot(
            clip={"x": 0, "y": 0, "width": VIEWPORT_W, "height": VIEWPORT_H})
    finally:
        page.close()


@app.post("/render-graph")
def render_graph():
    # Same guard as /render and /render-batch: robotics-og is public and
    # shares this SA (objectUser on the cards bucket) — never write from it.
    if OG_ONLY:
        return jsonify({"ok": False, "error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    card_id = (body.get("card_id") or "").strip()
    if not _CARD_ID_RE.match(card_id):
        return {"ok": False, "error": "bad card_id"}, 400
    # Cold instances have no local DuckDB until a pull (was a 500 otherwise).
    _pull_duckdb()
    _graph_doc_cache.clear()
    card = _load_card(card_id)
    if not card:
        return {"ok": False, "error": "unknown card_id"}, 404
    GRAPH_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    out = GRAPH_IMAGES_DIR / f"{card_id}.png"
    out.write_bytes(_render_graph_png(card_id, card))
    # Explicit single-card request → replace whatever poster is there.
    up = _upload_graph_png(card_id, out, overwrite=True)
    _log("graph_ok", card_id=card_id, bytes=out.stat().st_size, **up)
    return {"ok": True, "card_id": card_id, "bytes": out.stat().st_size, **up}, 200


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


def _utm_query() -> str:
    """The request's utm_* params as a '?…' suffix ('' if none), so the human
    redirect keeps the post's campaign tags and GA can attribute the visit.
    urlencode percent-escapes every value, so it's safe in href/JS too."""
    utm = [(k, v) for k, v in request.args.items(multi=True) if k.startswith("utm_")]
    return "?" + urlencode(utm) if utm else ""


def _og_page(card_id: str, title: str, description: str,
             generation: int | None = None, utm: str = "") -> str:
    """OG/Twitter tag page. Every injected value is HTML-escaped; card_id is
    already validated against _CARD_ID_RE by the route."""
    esc = html_mod.escape
    image_url = f"{CANONICAL_ORIGIN}/card-img/{card_id}.png"
    if generation:
        image_url += f"?g={generation}"
    page_url = f"{CANONICAL_ORIGIN}/card/{card_id}"
    redirect_path = f"/?card={card_id}" + (("&" + utm[1:]) if utm else "")
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
        utm = _utm_query()
        return redirect(CANONICAL_ORIGIN + ("/" + utm if utm else ""), code=302)

    return (
        _og_page(card_id, title, description, generation, _utm_query()),
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


@app.get("/graph-img/<card_id>.png")
def og_graph_image(card_id: str):
    """Public URL for the catalyst subgraph poster — the image soljet-postiz
    attaches. Mirrors /card-img/; served by the robotics-og service."""
    if not _CARD_ID_RE.match(card_id):
        return "not found", 404
    bucket = _cards_bucket()
    if bucket is None:
        return "not found", 404
    blob = bucket.blob(f"{STORAGE_GRAPHS_PREFIX}/{card_id}.png")
    if not blob.exists():
        return "not found", 404
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
    # Warm instances outlive batches; re-read the graph doc every batch.
    _graph_doc_cache.clear()
    # cards=false → posters only (e.g. a forced graph re-render backfill
    # without re-rendering every card PNG).
    cards_on = bool(body.get("cards", True))

    # Mode 4: explicit card_ids list — targeted re-render (e.g. only the
    # posters affected by a template fix), no date-range sweep needed.
    card_ids: list[str] = []
    if "card_ids" in body:
        raw_ids = body.get("card_ids")
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "card_ids must be a list"}), 400
        card_ids = [c for c in raw_ids if isinstance(c, str) and _CARD_ID_RE.match(c)]
        if not card_ids:
            # Never fall through to a full sweep: a typo'd targeted call with
            # force=true would otherwise re-render the whole catalog.
            return jsonify({"ok": False, "error": "no valid card_ids"}), 400
    if card_ids:
        rows = [(c,) for c in card_ids][: limit or None]
    elif from_id:
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
    if not card_ids:
        if limit:
            sql += f" LIMIT {limit}"

        import duckdb

        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()

    CARD_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Cloud Run instances are ephemeral — the local-disk check alone would
    # re-render the whole catalog on every cold start. One bucket listing
    # up front makes skip-detection survive instance churn.
    in_bucket: set[str] = set()
    if not force and cards_on:
        try:
            in_bucket = _bucket_png_ids()
        except Exception as exc:
            _log("bucket_list_failed", error=str(exc))

    # Catalyst subgraph posters ride the same loop: same skip rules, own
    # prefix. graphs=false in the body turns them off for a run.
    graphs_on = bool(body.get("graphs", True))
    graphs_in_bucket: set[str] = set()
    if graphs_on and not force:
        try:
            graphs_in_bucket = _bucket_graph_ids()
        except Exception as exc:
            _log("graph_bucket_list_failed", error=str(exc))
    if graphs_on:
        GRAPH_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    g_rendered = g_skipped = g_failed = g_uploaded = 0

    rendered, skipped, failed, uploaded, upload_skipped = 0, 0, 0, 0, 0
    for (entry_id,) in rows:
        card: dict[str, Any] | None = None    # loaded lazily, shared by card+graph
        out_path = CARD_IMAGES_DIR / f"{entry_id}.png"
        # NOTE: no `continue` in the card section — the graph block below must
        # still run for catalysts whose card PNG is already done (which is
        # every backfilled card), otherwise graphs never render at all.
        card_upload_needed = True
        if not cards_on:
            card_upload_needed = False
        elif not force and (out_path.exists() or entry_id in in_bucket):
            skipped += 1
            card_upload_needed = out_path.exists()   # local file → may need upload
        else:
            card = _load_card(entry_id)
            if not card:
                failed += 1
                card_upload_needed = False
            else:
                try:
                    out_path.write_bytes(_render_png(card))
                    rendered += 1
                except Exception as exc:
                    failed += 1
                    card_upload_needed = False
                    _log("render_failed", card_id=entry_id, error=str(exc))

        # Upload regardless of whether we just rendered or skipped — covers
        # the case where local PNGs exist but Storage doesn't (first cutover).
        if card_upload_needed:
            try:
                up = _upload_png(entry_id, out_path)
                if up.get("uploaded"):
                    uploaded += 1
                else:
                    upload_skipped += 1
            except Exception as exc:
                _log("upload_failed", card_id=entry_id, error=str(exc))

        if not graphs_on:
            continue
        g_path = GRAPH_IMAGES_DIR / f"{entry_id}.png"
        if not force and (g_path.exists() or entry_id in graphs_in_bucket):
            g_skipped += 1
            if not g_path.exists():
                continue
        else:
            card = card or _load_card(entry_id)
            if not card:
                g_failed += 1
                continue
            try:
                g_path.write_bytes(_render_graph_png(entry_id, card))
                g_rendered += 1
            except Exception as exc:
                g_failed += 1
                _log("graph_failed", card_id=entry_id, error=str(exc))
                continue
        try:
            if _upload_graph_png(entry_id, g_path, overwrite=force).get("uploaded"):
                g_uploaded += 1
        except Exception as exc:
            _log("graph_upload_failed", card_id=entry_id, error=str(exc))

    # Weekly recap cards ride every sweep; write-once makes repeats free.
    recap_summary: dict[str, Any] = {}
    if bool(body.get("recaps", True)):
        try:
            # Own flag: a `force` card/poster backfill must not rewrite the
            # write-once recap images consumers cache on the URL.
            recap_summary = _render_recaps(SECTOR, force=bool(body.get("recaps_force", False)))
        except Exception as exc:
            recap_summary = {"recaps_error": str(exc)}
            _log("recaps_failed", error=str(exc))

    duration = round(time.monotonic() - start, 2)
    _log("batch_ok", total=len(rows), rendered=rendered, skipped=skipped,
         failed=failed, uploaded=uploaded, upload_skipped=upload_skipped,
         graphs_rendered=g_rendered, graphs_skipped=g_skipped,
         graphs_failed=g_failed, graphs_uploaded=g_uploaded,
         duration_s=duration)
    return jsonify({
        "ok": True, "total": len(rows), "rendered": rendered, "skipped": skipped,
        "graphs_rendered": g_rendered, "graphs_skipped": g_skipped,
        "graphs_failed": g_failed, "graphs_uploaded": g_uploaded,
        "failed": failed, "uploaded": uploaded, "upload_skipped": upload_skipped,
        **recap_summary,
        "duration_s": duration,
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8081)))
