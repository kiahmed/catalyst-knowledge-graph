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


def _write_catalysts(cfg: RoboticsConfig, cards: list[dict[str, Any]]) -> int:
    """Upsert one doc per catalyst into {collection}/catalysts/{entry_id}.

    Returns the number of docs written.
    """
    client = _firestore_client(cfg)
    base = (
        client.collection(cfg.firestore_export.collection)
        .document(cfg.firestore_export.catalysts_subpath)
    )
    # We use a sub-collection structure: collection/{subpath}/<entry_id>.
    # Firestore models that as collection/{subpath}/items/{entry_id} when the
    # parent doc holds metadata. To keep things simple and addressable, we
    # store catalysts as a sub-collection of the `catalysts` doc:
    #   {collection}/catalysts/items/{entry_id}
    # The parent doc holds `count` + `updated_at` for cheap top-of-collection
    # reads on the frontend.
    items = base.collection("items")
    written = 0
    for card in cards:
        entry_id = card.get("card_id")  # export.py keys the id as "card_id"
        if not entry_id:
            continue
        items.document(entry_id).set(card, merge=True)
        written += 1

    # Top-of-subpath summary doc for cheap "any cards yet?" checks.
    base.set(
        {
            "count": written,
            "sector": cfg.sector,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        merge=True,
    )
    return written


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
        },
    )


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


def _upload_pngs(
    cfg: RoboticsConfig,
    cards: list[dict[str, Any]],
    images_dir: str,
) -> tuple[int, int, int]:
    """Upload local card PNGs to gs://{bucket}/{prefix}/{entry_id}.png.

    Returns (uploaded, skipped_existing, missing_local).
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

        blob = bucket.blob(f"{cfg.storage.cards_prefix}/{entry_id}.png")
        if blob.exists():
            skipped += 1
            continue

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
        written = _write_catalysts(cfg, cards)
        _write_graph(cfg, payload)
        summary["firestore_catalysts_written"] = written
        summary["firestore_collection"] = cfg.firestore_export.collection
        log.info(
            "firestore_export_done collection=%s catalysts=%d graph=ok",
            cfg.firestore_export.collection, written,
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
