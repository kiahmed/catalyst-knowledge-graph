"""Write card embeddings to Firestore (shared by `make embed` and ingest).

Vectors live in their OWN collection, {collection}/vectors/card_vectors/
{card_id} — never on the card docs. The web app downloads every card doc on
load; 768 floats per card doubled that payload (~9 → ~18 MB) and the site
hung (2026-11-04). Each vector doc carries the hybrid-search filter fields
(entity_names, rel_types, date) and enough to show a hit (card_id, headline).

A card needs (re-)embedding when its vector doc has no `embed_hash` or the
hash of its current text differs (relationships, entities or family changed)
— so re-running is cheap and only touches stale cards. Only cards that have
a Firestore card doc are embedded (the export owns card creation).
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb

from . import embed

log = logging.getLogger("robotics.embed")

_WRITE_BATCH = 50   # a 768-dim vector + its index entries is large; 400/commit hit "Transaction too big"
VECTORS_DOC = "vectors"
VECTORS_COLLECTION = "card_vectors"


def vectors_ref(fs_client: Any, collection: str) -> Any:
    """{collection}/vectors/card_vectors — the vector-index collection group."""
    return fs_client.collection(collection).document(VECTORS_DOC).collection(VECTORS_COLLECTION)


def sync(con: duckdb.DuckDBPyConnection, sector: str, fs_client: Any, items: Any, vectors: Any, client: Any,
         entry_ids: list[str] | None = None, *, only_stale: bool = True, dry_run: bool = False) -> dict[str, int]:
    """Embed the selected cards (default: every stale one) into `vectors`."""
    from google.cloud.firestore_v1.vector import Vector  # noqa: PLC0415

    inputs = embed.card_inputs(con, sector, entry_ids)
    cards = {d.id for d in items.select(["__name__"]).stream()}
    have = {d.id: (d.to_dict() or {}).get("embed_hash") for d in vectors.select(["embed_hash"]).stream()}
    todo = sorted(eid for eid, inp in inputs.items()
                  if eid in cards and (not only_stale or have.get(eid) != inp["hash"]))
    if dry_run or not todo:
        return {"selected": len(inputs), "to_embed": len(todo), "embedded": 0}
    vecs = embed.embed_texts(client, [inputs[e]["text"] for e in todo])
    for i in range(0, len(todo), _WRITE_BATCH):
        batch = fs_client.batch()
        for eid, vec in zip(todo[i:i + _WRITE_BATCH], vecs[i:i + _WRITE_BATCH]):
            inp = inputs[eid]
            batch.set(vectors.document(eid), {
                "card_id": eid, "headline": inp["headline"], "date": inp["date"],
                "embedding": Vector(vec), "embed_model": f"{embed.MODEL}@{embed.DIMENSIONS}",
                "embed_hash": inp["hash"], "entity_names": inp["entity_names"], "rel_types": inp["rel_types"]})
        batch.commit()
    return {"selected": len(inputs), "to_embed": len(todo), "embedded": len(todo)}


def prune(items: Any, vectors: Any, fs_client: Any) -> int:
    """Delete vector docs whose card no longer exists (merged/deleted cards)."""
    cards = {d.id for d in items.select(["__name__"]).stream()}
    gone = [d.id for d in vectors.select(["__name__"]).stream() if d.id not in cards]
    for i in range(0, len(gone), _WRITE_BATCH):
        batch = fs_client.batch()
        for cid in gone[i:i + _WRITE_BATCH]:
            batch.delete(vectors.document(cid))
        batch.commit()
    return len(gone)
