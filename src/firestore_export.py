"""Push catalysts + graph + card PNGs to Firestore + Firebase Storage.

Frontend (Firebase Hosting) reads from these. Local dev keeps cards.json
+ nginx as the read path; this module is gated by `cfg.firestore_export.enabled`
and `cfg.storage.enabled`.

Firestore layout (under the (default) database in `cfg.firestore.project`):

    {collection}/                                  (e.g. CKG-Robotics)
      catalysts/{entry_id}    one doc per catalyst — full card payload
      graph/{sector}          one doc per sector  — full graph projection + stats
      meta/run                one doc — last export timestamp + counts

Firebase Storage layout:

    gs://{cfg.storage.bucket}/{cfg.storage.cards_prefix}/{entry_id}.png

Read path: the bucket is PRIVATE (no public IAM). The frontend reads PNGs
through the Firebase Storage SDK — it derives the object path from entry_id
(`{cards_prefix}/{entry_id}.png`) and calls getDownloadURL(), gated by the
Storage Security Rules in tools/frontend-deploy/storage.rules. No per-card
URL is stored, which keeps catalyst docs immutable across re-uploads.

Idempotency:
- Catalyst docs use `set(merge=True)` so partial re-runs don't lose fields.
- Graph + meta docs use full overwrite (single-doc, latest wins).
- PNG uploads check `blob.exists()` first and skip when already uploaded.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import export
from .config import RoboticsConfig

log = logging.getLogger("robotics.firestore_export")


def _firestore_client(cfg: RoboticsConfig):
    """Lazy import — same pattern as src/ingest.py:_firestore_client."""
    from google.cloud import firestore

    fs = cfg.firestore
    kwargs: dict[str, Any] = {"project": fs.project} if fs.project else {}
    if fs.database and fs.database != "(default)":
        kwargs["database"] = fs.database
    return firestore.Client(**kwargs)


def _storage_client(cfg: RoboticsConfig):
    from google.cloud import storage

    return storage.Client(project=cfg.firestore.project or None)


# ── Firestore writes ───────────────────────────────────────────────


# ── Delta export state (what Firestore already has, tracked locally) ──
# One row per exported card: the hash of exactly what we last wrote. Lives in
# DuckDB, which goes back to GCS after every run, so the next run knows what
# changed WITHOUT reading Firestore. Only new/changed cards are written.
_STATE_DDL = """
CREATE TABLE IF NOT EXISTS export_state (
    doc_key      VARCHAR PRIMARY KEY,   -- 'catalysts/<card_id>'
    content_hash VARCHAR NOT NULL,      -- sha256 of the canonical card JSON
    exported_at  TIMESTAMP DEFAULT now()
)
"""
_BATCH = 400   # Firestore caps a batch at 500 writes


def _card_hash(card: dict[str, Any]) -> str:
    blob = json.dumps(card, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _full_sync() -> bool:
    """FIRESTORE_EXPORT_FULL_SYNC=true rewrites every card (e.g. after docs
    were deleted/edited by hand in Firestore, which the local state can't see)."""
    return os.environ.get("FIRESTORE_EXPORT_FULL_SYNC", "").strip().lower() in ("1", "true", "yes")


def _write_catalysts(cfg: RoboticsConfig, cards: list[dict[str, Any]]) -> dict[str, int]:
    """Write only new/changed cards to {collection}/catalysts/items/{card_id}.

    Change detection is local: each card's hash vs `export_state` in DuckDB.
    State is recorded per batch only AFTER Firestore commits it, so a failed
    write is retried on the next run (self-healing, like the old full sync).
    Returns {"written", "unchanged", "total"}.
    """
    import duckdb

    keyed = [(c["card_id"], c, _card_hash(c)) for c in cards if c.get("card_id")]
    con = duckdb.connect(cfg.duckdb_path)
    try:
        con.execute(_STATE_DDL)
        known = {} if _full_sync() else dict(con.execute(
            "SELECT doc_key, content_hash FROM export_state WHERE doc_key LIKE 'catalysts/%'"
        ).fetchall())
        changed = [(cid, c, h) for cid, c, h in keyed if known.get(f"catalysts/{cid}") != h]

        client = _firestore_client(cfg)
        base = (client.collection(cfg.firestore_export.collection)
                .document(cfg.firestore_export.catalysts_subpath))
        # Sub-collection layout: {collection}/catalysts/items/{card_id}; the
        # parent doc holds count + updated_at for cheap "any cards yet?" reads.
        items = base.collection("items")
        written = 0
        for i in range(0, len(changed), _BATCH):
            chunk = changed[i:i + _BATCH]
            batch = client.batch()
            for cid, card, _h in chunk:
                batch.set(items.document(cid), card, merge=True)
            batch.commit()
            con.executemany(
                "INSERT OR REPLACE INTO export_state (doc_key, content_hash, exported_at) "
                "VALUES (?, ?, now())",
                [(f"catalysts/{cid}", h) for cid, _c, h in chunk],
            )
            written += len(chunk)

        base.set(
            {
                "count": len(keyed),       # cards in the export, not this run's writes
                "sector": cfg.sector,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            merge=True,
        )
    finally:
        con.close()
    return {"written": written, "unchanged": len(keyed) - written, "total": len(keyed)}


def _write_graph(cfg: RoboticsConfig, payload: dict[str, Any]) -> None:
    """Overwrite the graph doc at {collection}/graph/sectors/{sector}."""
    client = _firestore_client(cfg)
    graph_doc = (
        client.collection(cfg.firestore_export.collection)
        .document(cfg.firestore_export.graph_subpath)
        .collection("sectors")
        .document(cfg.sector)
    )
    graph_doc.set(
        {
            "schema_version": payload.get("schema_version"),
            "generated_at": payload.get("generated_at"),
            "sector": payload.get("sector"),
            "stats": payload.get("stats"),
            "graph_config": payload.get("graph_config"),
            "graph": payload.get("graph"),
            "graph_insights": payload.get("graph_insights", []),
            "recap": payload.get("recap"),
        },
    )


def _write_recap_week(cfg: RoboticsConfig, recap: dict[str, Any] | None) -> str:
    """Archive the week's recap at {collection}/recaps/weeks/{YYYY-Www}.

    WRITE-ONCE: the first export of an ISO week (Monday's run) wins and later
    runs leave it alone — the recap images rendered from it are week-keyed
    and write-once too, so doc and images always agree. The graph doc's
    `recap` field stays the live, daily-refreshed copy.
    """
    if not recap or not recap.get("cards"):
        return "none"
    from google.api_core.exceptions import AlreadyExists

    client = _firestore_client(cfg)
    ref = (client.collection(cfg.firestore_export.collection)
           .document("recaps").collection("weeks").document(recap["week"]))
    try:
        ref.create({**recap, "sector": cfg.sector})
        return "created"
    except AlreadyExists:
        return "exists"
    except Exception as exc:          # recap is a bonus lane: never abort the export
        log.warning("recap_week_write_failed week=%s error=%s", recap.get("week"), exc)
        return "error"


def _write_meta(cfg: RoboticsConfig, summary: dict[str, Any]) -> None:
    """Last-run summary at {collection}/meta/runs/{sector}. Append-only."""
    client = _firestore_client(cfg)
    runs = (
        client.collection(cfg.firestore_export.collection)
        .document("meta")
        .collection("runs")
    )
    runs.document(cfg.sector).set(
        {
            "sector": cfg.sector,
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            **summary,
        },
    )


# ── Firebase Storage uploads ───────────────────────────────────────


def _file_md5_b64(path: str) -> str:
    """base64 md5 of a local file — the encoding GCS uses in blob.md5_hash."""
    import base64
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def _upload_pngs(
    cfg: RoboticsConfig,
    cards: list[dict[str, Any]],
    images_dir: str,
) -> tuple[int, int, int]:
    """Upload local card PNGs to gs://{bucket}/{prefix}/{entry_id}.png.

    Content-aware idempotence: a blob is skipped only when its md5 matches
    the local file. A re-rendered PNG (e.g. the 2026-07-05 2400x1260
    poster reframe) differs in content, so the sync overwrites the stale
    blob — existence alone is not "already synced".

    Returns (uploaded, skipped_same, missing_local); `uploaded` includes
    overwrites of stale blobs.
    """
    if not cfg.storage.bucket:
        log.warning("storage.bucket not set — skipping PNG upload")
        return 0, 0, 0

    client = _storage_client(cfg)
    bucket = client.bucket(cfg.storage.bucket)
    uploaded = skipped = missing = 0

    for card in cards:
        entry_id = card.get("card_id")  # export.py keys the id as "card_id"
        if not entry_id:
            continue
        local_path = os.path.join(images_dir, f"{entry_id}.png")
        if not os.path.exists(local_path):
            missing += 1
            continue

        # get_blob fetches metadata (None if absent); blob.md5_hash is the
        # base64 md5 GCS computed at upload time.
        blob = bucket.get_blob(f"{cfg.storage.cards_prefix}/{entry_id}.png")
        if blob is not None and blob.md5_hash == _file_md5_b64(local_path):
            skipped += 1
            continue
        if blob is None:
            blob = bucket.blob(f"{cfg.storage.cards_prefix}/{entry_id}.png")

        # Private bucket — no make_public(). Browsers read via the Firebase
        # Storage SDK gated by storage.rules; this SA write is IAM-governed.
        blob.upload_from_filename(local_path, content_type="image/png")
        uploaded += 1

    return uploaded, skipped, missing


# ── Public entry point ─────────────────────────────────────────────


def export_to_firestore(
    cfg: RoboticsConfig,
    images_dir: str = "/data/exports/card_images",
) -> dict[str, Any]:
    """Push the full export payload to Firestore + upload PNGs to Storage.

    Skipped (returns ``{"skipped": "<reason>"}``) when both flags are off.
    Either flag can be enabled independently; e.g. you can flip Firestore
    on first and leave PNG upload disabled until the bucket exists.
    """
    if not cfg.firestore_export.enabled and not cfg.storage.enabled:
        return {"skipped": "firestore_export + storage both disabled"}

    payload = export.build_payload(cfg)
    cards = payload["cards"]

    summary: dict[str, Any] = {
        "sector": cfg.sector,
        "cards": len(cards),
        "nodes": len(payload["graph"]["nodes"]),
        "edges": len(payload["graph"]["edges"]),
    }

    if cfg.firestore_export.enabled:
        cstats = _write_catalysts(cfg, cards)
        _write_graph(cfg, payload)
        summary["recap_week"] = _write_recap_week(cfg, payload.get("recap"))
        summary["firestore_catalysts_written"] = cstats["written"]
        summary["firestore_catalysts_unchanged"] = cstats["unchanged"]
        summary["firestore_collection"] = cfg.firestore_export.collection
        log.info(
            "firestore_export_done collection=%s catalysts_written=%d unchanged=%d graph=ok",
            cfg.firestore_export.collection, cstats["written"], cstats["unchanged"],
        )
    else:
        summary["firestore_skipped"] = True

    if cfg.storage.enabled:
        uploaded, skipped, missing = _upload_pngs(cfg, cards, images_dir)
        summary["pngs_uploaded"] = uploaded
        summary["pngs_skipped_existing"] = skipped
        summary["pngs_missing_local"] = missing
        summary["storage_bucket"] = cfg.storage.bucket
        log.info(
            "storage_upload_done bucket=%s uploaded=%d skipped=%d missing=%d",
            cfg.storage.bucket, uploaded, skipped, missing,
        )
    else:
        summary["storage_skipped"] = True

    if cfg.firestore_export.enabled:
        _write_meta(cfg, summary)

    return summary
