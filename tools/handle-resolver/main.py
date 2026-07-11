"""handle-resolver — on-demand social handle resolution service.

Local: docker compose service `handle-resolver` (port 8083), DuckDB-backed
cache (the shared entity_handles table in /data/robotics.duckdb).
Prod : Cloud Functions Gen 2, same entry point, Firestore-backed cache
(no DuckDB file at runtime) — serves spec contract B for soljet-postiz.

Request payload (JSON):
    {"entities": ["Figure AI", "Humanoid"], "channels": ["linkedin", "x"]}
        → contract B (docs/handle-resolution-spec.md): cache-first lookup,
          live verify-or-abstain resolve on miss, result cached.
          {"Figure AI": {"linkedin": "@figure-ai", "x": null}, ...}

    {"sweep": true, "limit": 25}
        → resolve every company entity in DuckDB still missing a handle
          row (local/DuckDB mode only). Re-runnable; abstains are cached
          so already-decided entities cost nothing.

Verify-or-abstain only — a null handle means "append no tag", never guess.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import threading

import functions_framework
import httpx
from flask import Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import db, handles  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("handle-resolver")

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "")
HANDLE_CACHE_COLLECTION = os.environ.get("HANDLE_CACHE_COLLECTION", "handle-cache")
SWEEP_DEFAULT_LIMIT = 25
ENTITY_BATCH_MAX = 50   # contract B: cap per-request live resolution work


# Sweeps are single-flight; lookups run concurrently on other threads.
_sweep_lock = threading.Lock()


def _duckdb_mode() -> bool:
    return bool(DUCKDB_PATH) and os.path.exists(DUCKDB_PATH)


# ── Firestore cache backend (prod) ─────────────────────────────────

def _fs_client():
    from google.cloud import firestore  # lazy — not installed/needed locally

    return firestore.Client(
        project=os.environ.get("GCP_PROJECT") or None,
        database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
    )


def _fs_doc_id(key: str) -> str:
    return key.replace("/", "__")


def _fs_get(fs, key: str) -> dict[str, dict[str, Any]]:
    snap = fs.collection(HANDLE_CACHE_COLLECTION).document(_fs_doc_id(key)).get()
    return snap.to_dict() or {} if snap.exists else {}


def _fs_put(fs, key: str, channel: str, result: handles.HandleResult) -> None:
    fs.collection(HANDLE_CACHE_COLLECTION).document(_fs_doc_id(key)).set(
        {
            channel: {
                "handle": result.handle,
                "confidence": result.confidence,
                "source": result.source,
                "comment": result.comment,
                "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "name_key": key,
        },
        merge=True,
    )


# ── Lookup (contract B) ────────────────────────────────────────────

def _lookup_duckdb(names: list[str], channels: list[str], client: httpx.Client) -> dict:
    con = db.connect(DUCKDB_PATH)
    try:
        db.init_schema(con)
        out: dict[str, dict[str, str | None]] = {}
        for name in names:
            key = handles.name_key(name)
            cached = db.get_handles(con, [key])
            aliases = _aliases_for(con, name)
            out[name] = {}
            for channel in channels:
                hit = cached.get((key, channel))
                if hit is not None and hit[1] != handles.SOURCE_BLOCKED:
                    out[name][channel] = hit[0]
                    continue
                result = handles.resolve_channel(
                    channel, name, aliases, client=client, search=handles.search_provider_from_env()
                )
                db.upsert_handle(
                    con, key, channel, result.handle, result.confidence,
                    result.source, result.comment,
                )
                out[name][channel] = result.handle
        return out
    finally:
        con.close()


def _aliases_for(con, name: str) -> list[str]:
    entity_id = db.find_entity_exact(con, name)
    if entity_id is None:
        return []
    rows = con.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", [entity_id]
    ).fetchall()
    return [r[0] for r in rows]


def _lookup_firestore(names: list[str], channels: list[str], client: httpx.Client) -> dict:
    fs = _fs_client()
    out: dict[str, dict[str, str | None]] = {}
    for name in names:
        key = handles.name_key(name)
        cached = _fs_get(fs, key)
        out[name] = {}
        for channel in channels:
            hit = cached.get(channel)
            if hit and hit.get("source") != handles.SOURCE_BLOCKED:
                out[name][channel] = hit.get("handle")
                continue
            result = handles.resolve_channel(
                channel, name, None, client=client, search=handles.search_provider_from_env()
            )
            _fs_put(fs, key, channel, result)
            out[name][channel] = result.handle
    return out


# ── Sweep (local backfill) ─────────────────────────────────────────

def _sweep(limit: int, client: httpx.Client) -> dict[str, Any]:
    search = handles.search_provider_from_env()
    # X is only resolvable via a search API; without one, sweep LinkedIn alone.
    channels = ["linkedin", "x"] if search else ["linkedin"]
    con = db.connect(DUCKDB_PATH)
    try:
        db.init_schema(con)
        counts: dict[str, Any] = {"resolved": 0, "abstained": 0, "blocked": 0}
        allow_direct = True   # flips off for the rest of the run on first 999
        for channel in channels:
            todo = db.entities_missing_handles(con, channel, limit)
            consecutive_blocked = 0
            for i, (name, aliases) in enumerate(todo):
                if i:
                    # Graceful gap between entities — SEARCH_DELAY_S on the
                    # search-API path; direct fallbacks add their own delay.
                    handles.polite_sleep(
                        handles.SEARCH_DELAY_S if search else handles.DEFAULT_FETCH_DELAY_S
                    )
                result = handles.resolve_channel(
                    channel, name, aliases,
                    client=client, search=search, allow_direct=allow_direct,
                )
                db.upsert_handle(
                    con, handles.name_key(name), channel,
                    result.handle, result.confidence, result.source, result.comment,
                )
                if result.handle:
                    counts["resolved"] += 1
                    consecutive_blocked = 0
                elif result.source == handles.SOURCE_BLOCKED:
                    counts["blocked"] += 1
                    if result.comment == handles.COMMENT_DIRECT_WALLED:
                        # LinkedIn walled this IP — skip direct fallbacks for
                        # the rest of the run; search-backed work continues.
                        allow_direct = False
                    # Only search-API failures predict the NEXT entity also
                    # failing (with a provider configured, direct-fetch walls
                    # don't) — so only they feed the circuit breaker.
                    if search is None or result.comment == handles.COMMENT_SEARCH_UNAVAILABLE:
                        consecutive_blocked += 1
                        if consecutive_blocked >= 3:
                            counts["stopped_early"] = True
                            log.warning("sweep circuit-breaker (%s): 3 consecutive blocked", channel)
                            break
                else:
                    counts["abstained"] += 1
                    consecutive_blocked = 0
            if counts.get("stopped_early"):
                break
        counts["remaining"] = {
            c: len(db.entities_missing_handles(con, c, 10_000)) for c in channels
        }
        return counts
    finally:
        con.close()


# ── Curation: unresolved report + manual set ───────────────────────

def _report_unresolved() -> dict:
    con = db.connect(DUCKDB_PATH)
    try:
        db.init_schema(con)
        return db.unresolved_handles_report(con)
    finally:
        con.close()


def _set_handles(entity: str, handle_map: dict[str, str | None]) -> dict:
    """Human-curated upsert. Any channel accepted (reddit, ...); handle may
    be null to force an abstain ('never tag this entity here')."""
    con = db.connect(DUCKDB_PATH)
    try:
        db.init_schema(con)
        key = handles.name_key(entity)
        stored = {}
        for channel, handle in handle_map.items():
            channel = channel.strip().lower()
            handle = handle.strip() if isinstance(handle, str) and handle.strip() else None
            db.upsert_handle(
                con, key, channel, handle, 1.0 if handle else 0.0,
                handles.SOURCE_HUMAN, "set manually",
            )
            stored[channel] = handle
        return {entity: stored}
    finally:
        con.close()


# ── HTTP entry point ───────────────────────────────────────────────

@functions_framework.http
def resolve_handles(request: Request):
    if request.method == "GET":
        return json.dumps({
            "service": "handle-resolver",
            "backend": "duckdb" if _duckdb_mode() else "firestore",
            "search": (
                type(p).__name__.replace("Config", "").lower()
                if (p := handles.search_provider_from_env()) else None
            ),
        }), 200, {"Content-Type": "application/json"}

    payload = request.get_json(silent=True) or {}

    client = httpx.Client(
        headers={"User-Agent": handles._UA}, timeout=15.0, follow_redirects=True
    )
    try:
        if payload.get("report") == "unresolved" or payload.get("set"):
            if not _duckdb_mode():
                return json.dumps({"error": "report/set require a local DuckDB (DUCKDB_PATH)"}), 400, {
                    "Content-Type": "application/json"
                }
            if payload.get("set"):
                spec = payload["set"]
                entity = spec.get("entity")
                handle_map = spec.get("handles") or {}
                if not entity or not handle_map:
                    return json.dumps(
                        {"error": 'set needs {"entity": name, "handles": {channel: handle}}'}
                    ), 400, {"Content-Type": "application/json"}
                result = _set_handles(entity, handle_map)
            else:
                result = _report_unresolved()
            return json.dumps(result), 200, {"Content-Type": "application/json"}

        if payload.get("sweep"):
            if not _duckdb_mode():
                return json.dumps({"error": "sweep requires a local DuckDB (DUCKDB_PATH)"}), 400, {
                    "Content-Type": "application/json"
                }
            limit = int(payload.get("limit") or SWEEP_DEFAULT_LIMIT)
            if not _sweep_lock.acquire(blocking=False):
                return json.dumps({"error": "a sweep is already running"}), 409, {
                    "Content-Type": "application/json"
                }
            try:
                counts = _sweep(limit, client)
            finally:
                _sweep_lock.release()
            log.info("sweep done: %s", counts)
            return json.dumps(counts), 200, {"Content-Type": "application/json"}

        names = payload.get("entities") or []
        channels = [c for c in (payload.get("channels") or list(handles.CHANNELS))
                    if c in handles.CHANNELS]
        if not names or not isinstance(names, list):
            return json.dumps({"error": "provide entities: [names] or sweep: true"}), 400, {
                "Content-Type": "application/json"
            }
        names = [str(n) for n in names[:ENTITY_BATCH_MAX]]
        lookup = _lookup_duckdb if _duckdb_mode() else _lookup_firestore
        result = lookup(names, channels, client)
        return json.dumps(result), 200, {"Content-Type": "application/json"}
    except Exception as exc:  # duckdb lock, firestore auth, ...
        log.exception("resolve failed")
        return json.dumps({"error": str(exc)}), 503, {"Content-Type": "application/json"}
    finally:
        client.close()
