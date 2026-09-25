#!/usr/bin/env python3
"""Seed this product's catalog item: config/products/items/<productId>.

Each CKG product (one per sector — CKG-Robotics → "robotics") gets one
catalog doc in the shared config/products/items catalog, alongside
arboryx-admin's `arboryx`. Firestore rules read `tier` from it to validate
users/{uid}/products/{productId} membership writes, so a product without
its catalog item can't record user membership from the browser.

    config/products/items/robotics = {
        productId:   "robotics",
        displayName: "Robotics",
        collection:  "CKG-Robotics",   # the product's data collection
        tier:        2,
        gateEnabled: false,
        createdAt / updatedAt: <server ts>,
    }

Runs on every `make deploy-frontend` (so a new product's first deploy
creates it) and standalone via `make seed-product`. Create-or-fill-missing
only: fields already on the doc are NEVER overwritten, so a tier or gate
changed later (here or by arboryx-admin) survives redeploys. Admin SDK —
bypasses rules; the browser can't write this doc.

Usage: seed_product.py <gcp_project> <sector> [--dry-run]
"""
from __future__ import annotations

import sys

DEFAULT_TIER = 2  # CKG sector products are 2nd-tier on the central userbase (arboryx = 1)


def product_doc(sector: str) -> tuple[str, dict]:
    pid = sector.strip().lower().replace(" ", "-")
    return pid, {
        "productId": pid,
        "displayName": sector.strip(),
        "collection": f"CKG-{sector.strip()}",
        "tier": DEFAULT_TIER,
        "gateEnabled": False,
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    project, sector = args
    dry = "--dry-run" in argv
    pid, want = product_doc(sector)
    path = f"config/products/items/{pid}"

    from google.cloud import firestore

    db = firestore.Client(project=project)
    ref = db.document(path)
    snap = ref.get()
    have = (snap.to_dict() or {}) if snap.exists else {}
    missing = {k: v for k, v in want.items() if k not in have}

    if not missing:
        print(f"  catalog {path}: present, tier={have.get('tier')} — unchanged")
        return 0
    print(f"  catalog {path}: {'create' if not snap.exists else 'fill'} {missing}")
    if dry:
        print("  [dry-run] no write")
        return 0
    missing["updatedAt"] = firestore.SERVER_TIMESTAMP
    if not snap.exists:
        missing["createdAt"] = firestore.SERVER_TIMESTAMP
    ref.set(missing, merge=True)
    print(f"  [OK] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
