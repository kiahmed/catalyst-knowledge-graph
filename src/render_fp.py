"""Render fingerprint — one short hash of exactly what a card's images show (docs/ticket-render-fingerprint.md).

Both images of a card — the card PNG (cards/<id>.png) and its graph poster (graphs/<id>.png) — are drawn only from
that card's own data: headline, date, subtitle, its live between-entity edges and their entities. Export writes this
hash into the card doc (`render_fp`); the render service stamps the hash it rendered from onto both images' storage
metadata; `make render-stale` lists a card when the two differ. So any merge, rename, delete, retype, evidence or
edge change on a card flags exactly that card — and fields the images never draw (hashtags, subtypes, event_type,
mechanism text, internal events, share text) never do.

Bump FP_VERSION when the card or poster TEMPLATE changes what is drawn: every card then re-renders once, on purpose.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

FP_VERSION = "1"


def fingerprint(card: dict[str, Any]) -> str:
    """`v<version>-<16 hex>` from the drawn fields of an exported card (src/export.py shape). Order-insensitive."""
    rels = sorted(
        (str(r.get("from") or ""), str(r.get("to") or ""), str(r.get("rel") or ""),
         round(float(r.get("confidence") or 0.0), 2), str(r.get("evidence_type") or ""),
         str(r.get("status") or "active"), bool(r.get("in_story")))
        for r in card.get("relationships") or [])
    ents = sorted(
        (str(e.get("name") or ""), str(e.get("ticker") or ""), str(e.get("type") or ""), bool(e.get("in_story")))
        for e in card.get("entities") or [])
    drawn = {"headline": card.get("headline") or "", "date": card.get("date") or "",
             "subtitle": card.get("subtitle") or "", "story_names_entities": bool(card.get("story_names_entities")),
             "relationships": rels, "entities": ents}
    blob = json.dumps(drawn, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"v{FP_VERSION}-{hashlib.sha256(blob.encode()).hexdigest()[:16]}"
