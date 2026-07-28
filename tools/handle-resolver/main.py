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

from src import db, handle_sweep as hs, handles  # noqa: E402

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

# Per-thread search-usage counter: each request wires its own DuckDB/Firestore
# bump function; handles._web_search calls the dispatcher on every live query.
_tl = threading.local()


def _usage_dispatch(provider: str) -> None:
    fn = getattr(_tl, "bump", None)
    if fn:
        fn(provider)


handles.search_usage_hook = _usage_dispatch


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


# ── Search budget: shared engine lives in src/handle_sweep.py ──────

_roll_cycles = hs.roll_cycles
_select_provider = hs.select_provider
_budget_report = hs.budget_report
_budget_guard = hs.budget_guard


# ── Lookup (contract B) ────────────────────────────────────────────

def _lookup_duckdb(names: list[str], channels: list[str], client: httpx.Client) -> dict:
    con = db.connect(DUCKDB_PATH)
    _tl.bump = lambda prov: db.bump_search_usage(con, prov)
    try:
        db.init_schema(con)
        search, _info = _budget_guard(con, len(names) * len(channels))
        out: dict[str, dict[str, str | None]] = {}
        for name in names:
            key = handles.name_key(name)
            cached = db.get_handles(con, [key])
            aliases = _aliases_for(con, name)
            context = _context_for(con, name)
            out[name] = {}
            for channel in channels:
                hit = cached.get((key, channel))
                if hit is not None and hit[1] != handles.SOURCE_BLOCKED:
                    out[name][channel] = handles.usable_handle(hit[0], hit[2])
                    continue
                if search is None:
                    # budget exhausted: serve cache only, don't resolve live
                    out[name][channel] = handles.usable_handle(hit[0], hit[2]) if hit else None
                    continue
                result = handles.resolve_channel(
                    channel, name, aliases, client=client,
                    search=search, context=context,
                )
                db.upsert_handle(
                    con, key, channel, result.handle, result.confidence,
                    result.source, result.comment,
                )
                out[name][channel] = handles.usable_handle(result.handle, result.confidence)
        return out
    finally:
        _tl.bump = None
        con.close()


def _context_for(con, name: str):
    return hs.context_for(con, name)


def _aliases_for(con, name: str) -> list[str]:
    return hs.aliases_for(con, name)


def _lookup_firestore(names: list[str], channels: list[str], client: httpx.Client) -> dict:
    fs = _fs_client()
    from google.cloud import firestore as _fsm

    def _bump(provider: str) -> None:
        fs.collection("search-config").document(provider).set(
            {"used_this_cycle": _fsm.Increment(1), "provider": provider},
            merge=True,
        )
    _tl.bump = _bump
    out: dict[str, dict[str, str | None]] = {}
    for name in names:
        key = handles.name_key(name)
        cached = _fs_get(fs, key)
        out[name] = {}
        for channel in channels:
            hit = cached.get(channel)
            if hit and hit.get("source") != handles.SOURCE_BLOCKED:
                out[name][channel] = handles.usable_handle(
                    hit.get("handle"), hit.get("confidence"))
                continue
            result = handles.resolve_channel(
                channel, name, None, client=client, search=handles.search_provider_from_env()
            )
            _fs_put(fs, key, channel, result)
            out[name][channel] = result.handle
    _tl.bump = None
    return out


# ── Sweep (local backfill) ─────────────────────────────────────────

def _sweep(limit: int, client: httpx.Client) -> dict[str, Any]:
    con = db.connect(DUCKDB_PATH)
    _tl.bump = lambda prov: db.bump_search_usage(con, prov)
    try:
        db.init_schema(con)
        return hs.sweep(con, client, limit)
    finally:
        _tl.bump = None
        con.close()


# ── Re-audit: re-verify existing rows under the current logic ──────

def _reaudit(params: dict, client: httpx.Client) -> dict[str, Any]:
    """Re-resolve cached rows (fresh SERP + context + ambiguity guard) and
    overwrite them with the new verdict. Safety rails:
      - 'human' and 'blocked' rows are never touched
      - a blocked re-resolution (quota/network) NEVER overwrites the old
        value — the row keeps its handle and is retried next run
      - every processed row is stamped audited_at, so re-runs resume where
        the last one stopped (FORCE re-audits stamped rows again)
    """
    channels = [c for c in (params.get("channels") or list(handles.CHANNELS))
                if c in handles.CHANNELS]
    max_conf = float(params.get("max_confidence") or 0.9)
    limit = int(params.get("limit") or 50)
    force = bool(params.get("force"))
    names = None
    raw_names = params.get("entities")
    if raw_names:
        if isinstance(raw_names, str):
            raw_names = raw_names.split(";")
        names = [handles.name_key(str(n)) for n in raw_names if str(n).strip()]

    con = db.connect(DUCKDB_PATH)
    _tl.bump = lambda prov: db.bump_search_usage(con, prov)
    try:
        db.init_schema(con)
        search, binfo = _budget_guard(con, limit)
        if search is None:
            return binfo
        remaining = binfo.get("searches_remaining")
        if remaining is not None and remaining < limit:
            limit = max(1, remaining)
        todo = db.handles_for_reaudit(con, channels, max_conf, names, limit, force)
        counts: dict[str, Any] = {
            "audited": 0, "confirmed": 0, "changed": 0,
            "now_abstained": 0, "blocked_retry": 0,
        }
        changes: list[dict[str, Any]] = []
        consecutive_blocked = 0
        for i, (key, channel, old_handle, _old_conf, _old_source) in enumerate(todo):
            if i:
                handles.polite_sleep(handles.SEARCH_DELAY_S)
            ent = db.entity_by_name_key(con, key)
            name = ent[0] if ent else key
            result = handles.resolve_channel(
                channel, name, _aliases_for(con, name),
                client=client, search=search, allow_direct=False,
                context=_context_for(con, name),
            )
            if result.source == handles.SOURCE_BLOCKED:
                if result.comment in (handles.COMMENT_DIRECT_SKIPPED,
                                      handles.COMMENT_DIRECT_WALLED):
                    # SERP is empty now and direct fetch is off during audits:
                    # no evidence either way — keep the prior value, stamp the
                    # row so the audit moves on instead of looping on it.
                    db.mark_handle_audited(con, key, channel)
                    counts["kept_no_evidence"] = counts.get("kept_no_evidence", 0) + 1
                    counts["audited"] += 1
                    consecutive_blocked = 0
                    continue
                # Quota/network — keep the existing value, retry next run.
                counts["blocked_retry"] += 1
                consecutive_blocked += 1
                if consecutive_blocked >= 3:
                    counts["stopped_early"] = True
                    log.warning("reaudit circuit-breaker: 3 consecutive blocked")
                    break
                continue
            consecutive_blocked = 0
            was = old_handle or "abstain"
            db.upsert_handle(
                con, key, channel, result.handle, result.confidence,
                result.source, f"reaudit (was {was}): {result.comment}",
            )
            db.mark_handle_audited(con, key, channel)
            counts["audited"] += 1
            if result.handle == old_handle:
                counts["confirmed"] += 1
            else:
                counts["changed"] += 1
                if result.handle is None:
                    counts["now_abstained"] += 1
                changes.append({
                    "entity": key, "channel": channel,
                    "was": old_handle, "now": result.handle,
                    "why": (result.comment or "")[:120],
                })
        counts["remaining"] = len(
            db.handles_for_reaudit(con, channels, max_conf, names, 100_000, force=False)
        )
        counts["changes"] = changes
        counts["budget"] = binfo
        return counts
    finally:
        _tl.bump = None
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

    if payload.get("budget"):
        if not _duckdb_mode():
            return json.dumps({"error": "budget report requires a local DuckDB"}), 400, {
                "Content-Type": "application/json"
            }
        con = db.connect(DUCKDB_PATH)
        try:
            db.init_schema(con)
            return json.dumps(_budget_report(con)), 200, {"Content-Type": "application/json"}
        finally:
            con.close()

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

        if payload.get("reaudit") is not None:
            if not _duckdb_mode():
                return json.dumps({"error": "reaudit requires a local DuckDB (DUCKDB_PATH)"}), 400, {
                    "Content-Type": "application/json"
                }
            spec = payload["reaudit"] if isinstance(payload["reaudit"], dict) else {}
            if not _sweep_lock.acquire(blocking=False):
                return json.dumps({"error": "a sweep/reaudit is already running"}), 409, {
                    "Content-Type": "application/json"
                }
            try:
                counts = _reaudit(spec, client)
            finally:
                _sweep_lock.release()
            log.info("reaudit done: %s", {k: v for k, v in counts.items() if k != "changes"})
            return json.dumps(counts), 200, {"Content-Type": "application/json"}

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
