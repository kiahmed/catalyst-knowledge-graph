"""Write card embeddings to Firestore (shared by `make embed` and ingest).

A card needs (re-)embedding when its doc has no `embed_hash` or the hash of
its current text differs (relationships, entities or family changed) — so
re-running is cheap and only touches stale cards.
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb

from . import embed

log = logging.getLogger("robotics.embed")


def _existing(items: Any) -> dict[str, str | None]:
    """card_id → stored embed_hash (None when the doc has no embedding yet)."""
    return {d.id: (d.to_dict() or {}).get("embed_hash") for d in items.select(["embed_hash"]).stream()}


def sync(con: duckdb.DuckDBPyConnection, sector: str, fs_client: Any, items: Any, client: Any,
         entry_ids: list[str] | None = None, *, only_stale: bool = True, dry_run: bool = False) -> dict[str, int]:
    """Embed the selected cards (default: every stale one) and store vector +
    filter fields on their docs. Cards without a Firestore doc are skipped —
    the export creates docs, this only enriches them."""
    from google.cloud.firestore_v1.vector import Vector  # noqa: PLC0415

    inputs = embed.card_inputs(con, sector, entry_ids)
    have = _existing(items)
    todo = sorted(eid for eid, inp in inputs.items()
                  if eid in have and (not only_stale or have[eid] != inp["hash"]))
    if dry_run or not todo:
        return {"selected": len(inputs), "to_embed": len(todo), "embedded": 0}
    vectors = embed.embed_texts(client, [inputs[e]["text"] for e in todo])
    for i in range(0, len(todo), 400):   # Firestore batch cap is 500 writes
        batch = fs_client.batch()
        for eid, vec in zip(todo[i:i + 400], vectors[i:i + 400]):
            inp = inputs[eid]
            batch.set(items.document(eid), {"embedding": Vector(vec), "embed_model": f"{embed.MODEL}@{embed.DIMENSIONS}",
                                            "embed_hash": inp["hash"], "entity_names": inp["entity_names"],
                                            "rel_types": inp["rel_types"]}, merge=True)
        batch.commit()
    return {"selected": len(inputs), "to_embed": len(todo), "embedded": len(todo)}
