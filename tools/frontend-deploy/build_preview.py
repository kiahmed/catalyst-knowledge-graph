#!/usr/bin/env python3
"""Snapshot the newest few catalysts from Firestore into frontend/preview.json.

A PUBLIC teaser. The deployed landing page shows these few real cards to a
visitor who has not signed in yet — a peek at real data. The full catalyst
set + the graph stay auth-gated in Firestore (CKG-<sector>); this file is a
small, static, public subset baked into the bundle at deploy time.

Run by tools/frontend-deploy/deploy.sh. Auth: GOOGLE_APPLICATION_CREDENTIALS
(the firebase-deployer service-account key) — same credential as the deploy.

Usage: build_preview.py <project_id> <sector> <out_path> [count]
"""
from __future__ import annotations

import json
import sys

from google.cloud import firestore

DEFAULT_COUNT = 4


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: build_preview.py <project_id> <sector> <out_path> [count]")
        return 2
    project, sector, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    count = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_COUNT
    collection = f"CKG-{sector}"

    db = firestore.Client(project=project)
    items = db.collection(collection).document("catalysts").collection("items")

    # Newest first. `date` is ISO (YYYY-MM-DD) so a string sort is correct;
    # a single-field order_by needs no composite index. Fall back to an
    # unordered read if order_by fails for any reason.
    try:
        docs = list(
            items.order_by("date", direction=firestore.Query.DESCENDING)
            .limit(count)
            .stream()
        )
    except Exception as e:  # noqa: BLE001
        print(f"  order_by failed ({e}); taking an unordered sample")
        docs = list(items.limit(count).stream())

    cards = [d.to_dict() for d in docs]

    # generated_at from the graph doc, for an accurate "as of" on the page.
    generated_at = None
    try:
        gdoc = (
            db.collection(collection)
            .document("graph")
            .collection("sectors")
            .document(sector)
            .get()
        )
        if gdoc.exists:
            generated_at = (gdoc.to_dict() or {}).get("generated_at")
    except Exception:  # noqa: BLE001
        pass

    # Same envelope shape as cards.json, but graph is intentionally empty —
    # the graph tab is unreachable until sign-in.
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "sector": sector,
        "stats": None,
        "graph_config": {},
        "cards": cards,
        "graph": {"nodes": [], "edges": []},
        "graph_insights": [],
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  preview.json — {len(cards)} card(s) from {collection}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
