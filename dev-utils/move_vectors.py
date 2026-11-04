"""One-time fix (2026-11-04): move card vectors OFF the card docs.

The web app downloads every card doc on load; the 768-float `embedding` on
each doc doubled that payload and the site hung. This copies each existing
vector (no re-embedding, no API cost) to {collection}/vectors/card_vectors/
{card_id}, verifies the copy, then deletes embedding/embed_hash/embed_model
from the card docs. Safe to re-run (copies are idempotent; strip only
touches docs that still carry the fields).

Usage: make vectors-move
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import firestore  # noqa: E402

from src.config import load_config  # noqa: E402
from src.embed_sync import vectors_ref  # noqa: E402

_BATCH = 50


def main() -> int:
    cfg = load_config()
    db_kw = {"database": cfg.firestore.database} if cfg.firestore.database not in ("", "(default)") else {}
    fs = firestore.Client(project=cfg.firestore.project or None, **db_kw)
    items = fs.collection(cfg.firestore_export.collection).document(cfg.firestore_export.catalysts_subpath) \
        .collection("items")
    vectors = vectors_ref(fs, cfg.firestore_export.collection)
    carrying = [d for d in items.stream() if (d.to_dict() or {}).get("embedding") is not None]
    print(f"{len(carrying)} card doc(s) carry a vector", file=sys.stderr)

    for i in range(0, len(carrying), _BATCH):
        batch = fs.batch()
        for d in carrying[i:i + _BATCH]:
            x = d.to_dict()
            batch.set(vectors.document(d.id), {
                "card_id": d.id, "headline": x.get("headline"), "date": x.get("date"),
                "embedding": x["embedding"], "embed_model": x.get("embed_model"), "embed_hash": x.get("embed_hash"),
                "entity_names": x.get("entity_names", []), "rel_types": x.get("rel_types", [])})
        batch.commit()
    have = {d.id for d in vectors.select(["embed_hash"]).stream()}
    missing = [d.id for d in carrying if d.id not in have]
    if missing:
        print(f"ABORT: {len(missing)} vector(s) not copied (e.g. {missing[:3]}); card docs left untouched",
              file=sys.stderr)
        return 2
    print(f"copied {len(carrying)} → {cfg.firestore_export.collection}/vectors/card_vectors", file=sys.stderr)

    for i in range(0, len(carrying), 100):
        batch = fs.batch()
        for d in carrying[i:i + 100]:
            batch.update(items.document(d.id), {"embedding": firestore.DELETE_FIELD,
                                                "embed_hash": firestore.DELETE_FIELD,
                                                "embed_model": firestore.DELETE_FIELD})
        batch.commit()
    print(f"stripped vectors from {len(carrying)} card doc(s) — reload the site", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
