#!/usr/bin/env python3
"""pipeline_status.py — one table: where every Robotics entry is, per stage.

Stages: upstream Arboryx findings → ingested (DuckDB) → exported
(cards.json) → Firestore CKG items, plus a render section (local PNGs by
format, bucket PNGs by sync state vs local).

Run via `make ingest-render-status` (or directly). Cloud counts need
GOOGLE_APPLICATION_CREDENTIALS (SA key) in the environment — rows degrade
to "n/a" when auth or the docker stack is unavailable, never crash.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = REPO_ROOT / "data" / "exports" / "card_images"
CARDS_JSON = REPO_ROOT / "data" / "exports" / "cards.json"
SECTOR = os.environ.get("SECTOR", "Robotics")
PROJECT = os.environ.get("GCP_PROJECT", "")
POSTER_DIMS = (2400, 1260)
FS = "https://firestore.googleapis.com/v1"


def token() -> str | None:
    key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key or not os.path.exists(key):
        return None
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        c = service_account.Credentials.from_service_account_file(
            key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        c.refresh(Request())
        return c.token
    except Exception:
        return None


def _post(url: str, tok: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _get(url: str, tok: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fs_count(tok: str, parent: str, collection: str, where: dict | None = None) -> int | None:
    q: dict = {"from": [{"collectionId": collection}]}
    if where:
        q["where"] = where
    try:
        d = _post(f"{FS}/projects/{PROJECT}/databases/(default)/documents{parent}:runAggregationQuery",
                  tok, {"structuredAggregationQuery": {"structuredQuery": q,
                        "aggregations": [{"count": {}, "alias": "n"}]}})
        return int(d[0]["result"]["aggregateFields"]["n"]["integerValue"])
    except Exception:
        return None


def duckdb_count() -> int | None:
    try:
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "duckdb", "duckdb",
             "/data/robotics.duckdb", "-json", "SELECT COUNT(*) AS n FROM catalysts;"],
            capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        return int(json.loads(out.stdout)[0]["n"]) if out.returncode == 0 else None
    except Exception:
        return None


def cloud_duckdb_count(tok: str) -> int | None:
    """Catalyst count from the AUTHORITATIVE GCS DuckDB (prod owns state
    since the 2026-07-28 handover). Downloads to data/.prod-status.duckdb,
    cached by GCS generation so repeat runs are free."""
    bucket = os.environ.get("DUCKDB_GCS_BUCKET", "robotics-data")
    try:
        meta = _get(f"https://storage.googleapis.com/storage/v1/b/{bucket}"
                    f"/o/robotics.duckdb?fields=generation", tok)
        gen = meta["generation"]
        cache = REPO_ROOT / "data" / ".prod-status.duckdb"
        gen_file = cache.with_suffix(".gen")
        if not (cache.exists() and gen_file.exists() and gen_file.read_text() == gen):
            req = urllib.request.Request(
                f"https://storage.googleapis.com/storage/v1/b/{bucket}"
                f"/o/robotics.duckdb?alt=media",
                headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                cache.write_bytes(r.read())
            gen_file.write_text(gen)
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "duckdb", "duckdb",
             "/data/.prod-status.duckdb", "-readonly", "-json",
             "SELECT COUNT(*) AS n FROM catalysts;"],
            capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        return int(json.loads(out.stdout)[0]["n"]) if out.returncode == 0 else None
    except Exception:
        return None


def cards_json_count() -> tuple[int | None, str]:
    try:
        d = json.loads(CARDS_JSON.read_text())
        return len(d.get("cards", [])), d.get("generated_at", "?")
    except Exception:
        return None, "?"


def png_dims(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", head[16:24])
    except OSError:
        return None


def local_pngs() -> tuple[dict[str, str], int, int]:
    """→ ({card_id: md5_b64}, poster_count, old_count)."""
    md5s: dict[str, str] = {}
    poster = old = 0
    for p in sorted(IMAGES_DIR.glob("*.png")):
        if png_dims(p) == POSTER_DIMS:
            poster += 1
        else:
            old += 1
        md5s[p.stem] = base64.b64encode(hashlib.md5(p.read_bytes()).digest()).decode()
    return md5s, poster, old


def bucket_pngs(tok: str, bucket: str, prefix: str) -> dict[str, str] | None:
    """→ {card_id: md5_b64} or None on failure."""
    try:
        items, page = {}, None
        while True:
            url = (f"https://storage.googleapis.com/storage/v1/b/{bucket}/o"
                   f"?prefix={prefix}/&fields=items(name,md5Hash),nextPageToken"
                   + (f"&pageToken={page}" if page else ""))
            d = _get(url, tok)
            for it in d.get("items", []):
                items[Path(it["name"]).stem] = it.get("md5Hash", "")
            page = d.get("nextPageToken")
            if not page:
                return items
    except Exception:
        return None


def fmt(n) -> str:
    return "n/a" if n is None else str(n)


def main() -> int:
    tok = token()
    upstream = fs_count(tok, "", "findings",
                        {"fieldFilter": {"field": {"fieldPath": "category"},
                                         "op": "EQUAL",
                                         "value": {"stringValue": SECTOR}}}) if tok else None
    ck_items = fs_count(tok, f"/CKG-{SECTOR}/catalysts", "items") if tok else None
    ducked = duckdb_count()
    cloud_ducked = cloud_duckdb_count(tok) if tok else None
    exported, gen_at = cards_json_count()
    local_md5, poster, old = local_pngs()
    # Prod names it CARDS_BUCKET (.env.prod); local dev names it
    # STORAGE_BUCKET (.env). Accept either.
    bucket = os.environ.get("CARDS_BUCKET") or os.environ.get("STORAGE_BUCKET", "")
    prefix = os.environ.get("STORAGE_CARDS_PREFIX", "cards")
    bucket_md5 = bucket_pngs(tok, bucket, prefix) if tok else None

    local_total = len(local_md5)
    if bucket_md5 is not None:
        in_sync = sum(1 for cid, m in local_md5.items() if bucket_md5.get(cid) == m)
        stale = sum(1 for cid in local_md5 if cid in bucket_md5 and bucket_md5[cid] != local_md5[cid])
        bucket_total = len(bucket_md5)
    else:
        in_sync = stale = bucket_total = None

    gap = (lambda a, b: f"  ({a - b} behind)" if a is not None and b is not None and a > b else "")

    print(f"\nPipeline status — {SECTOR}   (poster format = {POSTER_DIMS[0]}x{POSTER_DIMS[1]})")
    print("  local = dev copy (frozen since the 2026-07-28 cloud handover)")
    print("  cloud = authoritative (GCS DuckDB / Firestore / Storage)")
    print("─" * 66)
    print(f"{'Stage':30}{'local':>10}{'cloud':>10}")
    print(f"{'Upstream findings (Arboryx)':30}{'':>10}{fmt(upstream):>10}")
    print(f"{'Ingested (DuckDB catalysts)':30}{fmt(ducked):>10}{fmt(cloud_ducked):>10}"
          f"{gap(upstream, cloud_ducked)}")
    print(f"{'Exported cards':30}{fmt(exported):>10}{fmt(ck_items):>10}"
          f"  (local gen {gen_at})")
    print(f"{'Firestore CKG items':30}{'':>10}{fmt(ck_items):>10}{gap(cloud_ducked, ck_items)}")
    print("─" * 66)
    print(f"{'Render':30}{'local':>10}{'bucket':>10}")
    print(f"{'  poster format':30}{poster:>10}{'':>10}")
    print(f"{'  old format (needs reframe)':30}{old:>10}{'':>10}")
    missing_local = ducked - local_total if ducked is not None else None
    missing_cloud = (cloud_ducked - bucket_total
                     if cloud_ducked is not None and bucket_total is not None else None)
    print(f"{'  total PNGs':30}{local_total:>10}{fmt(bucket_total):>10}")
    print(f"{'  missing (vs own DB)':30}{fmt(missing_local):>10}{fmt(missing_cloud):>10}")
    print(f"{'  in-sync (local∩bucket md5)':30}{'':>10}{fmt(in_sync):>10}")
    print(f"{'  stale in bucket (md5 differs)':30}{'':>10}{fmt(stale):>10}")
    print("─" * 66)
    if tok is None:
        print("cloud rows n/a — set GOOGLE_APPLICATION_CREDENTIALS to an SA key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
