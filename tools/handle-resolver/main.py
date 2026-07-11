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


# ── Search budget: cycle roll, provider selection, pre-flight ──────

import calendar
from datetime import date, timedelta


def _next_refill(cycle_start: date, refill_day: int) -> date:
    """First refill date strictly after cycle_start (same day next month,
    clamped to month length)."""
    y, m = cycle_start.year, cycle_start.month
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return date(y, m, min(refill_day, calendar.monthrange(y, m)[1]))


def _roll_cycles(con) -> list[dict]:
    """Roll any provider whose refill date passed; return fresh rows with
    computed remaining + next_refill."""
    today = date.today()
    out = []
    for row in db.get_search_providers(con):
        cs, rd = row["cycle_start"], row["refill_day"]
        if cs is not None and not isinstance(cs, date):
            cs = date.fromisoformat(str(cs))
        if rd and cs:
            nxt = _next_refill(cs, rd)
            while nxt <= today:          # catch up over multiple idle months
                cs = nxt
                nxt = _next_refill(cs, rd)
            if cs != row["cycle_start"]:
                db.reset_search_cycle(con, row["provider"], cs)
                row["used_this_cycle"], row["cycle_start"] = 0, cs
            row["next_refill"] = str(nxt)
        else:
            row["next_refill"] = None    # one-time credits — never refills
        quota = row["monthly_quota"]
        row["remaining"] = (max(0, quota - row["used_this_cycle"])
                            if quota is not None else None)
        out.append(row)
    return out


def _env_key_for(provider: str) -> bool:
    return bool({
        "brave": os.environ.get("BRAVE_API_KEY", ""),
        "serper": os.environ.get("SERPER_API_KEY", ""),
        "searchapi": os.environ.get("SEARCHAPI_API_KEY", ""),
        "cse": os.environ.get("GOOGLE_CSE_API_KEY", "") and os.environ.get("GOOGLE_CSE_ID", ""),
    }.get(provider, "").strip())


def _make_config(provider: str):
    if provider == "brave":
        return handles.BraveConfig(os.environ["BRAVE_API_KEY"].strip())
    if provider == "serper":
        return handles.SerperConfig(os.environ["SERPER_API_KEY"].strip())
    if provider == "searchapi":
        return handles.SearchApiConfig(os.environ["SEARCHAPI_API_KEY"].strip())
    if provider == "cse":
        return handles.CseConfig(os.environ["GOOGLE_CSE_API_KEY"].strip(),
                                 os.environ["GOOGLE_CSE_ID"].strip())
    return None


def _select_provider(con) -> tuple[str | None, object | None, dict | None]:
    """(name, config, budget_row) — first enabled provider by priority with
    a configured key and remaining budget (unknown quota counts as OK)."""
    for row in _roll_cycles(con):
        if not row["enabled"] or not _env_key_for(row["provider"]):
            continue
        if row["remaining"] is not None and row["remaining"] <= 0:
            continue
        return row["provider"], _make_config(row["provider"]), row
    return None, None, None


def _budget_report(con) -> dict:
    rows = _roll_cycles(con)
    active, _cfg, _row = _select_provider(con)
    return {
        "active_provider": active,
        "providers": [
            {k: (str(v) if isinstance(v, date) else v) for k, v in r.items()}
            for r in rows
        ],
    }


def _budget_guard(con, estimated: int) -> tuple[object | None, dict]:
    """Pre-flight: pick provider, warn/clamp against remaining budget.
    Returns (search_config, info). search_config None = refuse."""
    name, cfg, row = _select_provider(con)
    if cfg is None:
        rows = _roll_cycles(con)
        refills = [r["next_refill"] for r in rows if r["next_refill"]]
        return None, {
            "error": "no search provider has remaining budget",
            "next_refill": min(refills) if refills else None,
            "budget": _budget_report(con),
        }
    info: dict = {"provider": name}
    if row["remaining"] is not None:
        info["searches_remaining"] = row["remaining"]
        info["next_refill"] = row["next_refill"]
        if estimated > row["remaining"]:
            info["warning"] = (
                f"requested work (~{estimated} searches) exceeds the {row['remaining']} "
                f"left this cycle on '{name}' — clamped; rest resumes after "
                f"{row['next_refill']}"
            )
    return cfg, info


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


def _context_for(con, name: str) -> set[str] | None:
    """Context bag for disambiguation: sector + partner entity names +
    headlines of the cards this entity appears in. DuckDB mode only."""
    entity_id = db.find_entity_exact(con, name)
    if entity_id is None:
        return None
    partners = [r[0] for r in con.execute(
        """
        SELECT DISTINCT e2.name
        FROM relationships r
        JOIN entities e2 ON e2.entity_id IN (r.entity_a_id, r.entity_b_id)
        WHERE ? IN (r.entity_a_id, r.entity_b_id) AND e2.entity_id != ?
        LIMIT 30
        """, [entity_id, entity_id]).fetchall()]
    headlines = [r[0] for r in con.execute(
        """
        SELECT DISTINCT c.headline
        FROM catalysts c JOIN relationships r ON r.catalyst_id = c.catalyst_id
        WHERE ? IN (r.entity_a_id, r.entity_b_id)
        LIMIT 20
        """, [entity_id]).fetchall()]
    sector = os.environ.get("SECTOR", "Robotics")
    # Sector words are the strongest industry signal — include obvious forms.
    return handles.context_terms_from(f"{sector} robot robots", partners, headlines)


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
        search, binfo = _budget_guard(con, limit * 2)
        if search is None:
            return binfo
        remaining = binfo.get("searches_remaining")
        if remaining is not None and remaining < limit * 2:
            limit = max(1, remaining // 2)
        channels = ["linkedin", "x"]
        counts: dict[str, Any] = {"resolved": 0, "abstained": 0, "blocked": 0,
                                  "budget": binfo}
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
                    context=_context_for(con, name),
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
