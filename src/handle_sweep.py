"""Shared handle-sweep + search-budget engine.

Used by BOTH consumers of the entity_handles cache:
  - tools/handle-resolver (local compose service): HTTP sweep/reaudit/budget
  - tools/robotics-ingest (prod Cloud Function): in-process bounded sweep
    after each ingest, before export — the prod mirror of the local
    `make ingest` → sweep → export chain.

All functions take an open DuckDB connection; callers own its lifecycle.
Provider selection, cycle rolling, and usage counting live here so the
budget registry (search_providers table) is enforced identically local
and prod.
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import date
from typing import Any

import httpx

from . import db, handles

log = logging.getLogger(__name__)


# ── Budget: cycle roll, provider selection, pre-flight ─────────────

def next_refill(cycle_start: date, refill_day: int) -> date:
    """First refill date strictly after cycle_start (same day next month,
    clamped to month length)."""
    y, m = cycle_start.year, cycle_start.month
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return date(y, m, min(refill_day, calendar.monthrange(y, m)[1]))


def roll_cycles(con) -> list[dict]:
    """Roll any provider whose refill date passed; return fresh rows with
    computed remaining + next_refill."""
    today = date.today()
    out = []
    for row in db.get_search_providers(con):
        cs, rd = row["cycle_start"], row["refill_day"]
        if cs is not None and not isinstance(cs, date):
            cs = date.fromisoformat(str(cs))
        if rd and cs:
            nxt = next_refill(cs, rd)
            while nxt <= today:          # catch up over multiple idle months
                cs = nxt
                nxt = next_refill(cs, rd)
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


def env_key_for(provider: str) -> bool:
    return bool({
        "brave": os.environ.get("BRAVE_API_KEY", ""),
        "serper": os.environ.get("SERPER_API_KEY", ""),
        "searchapi": os.environ.get("SEARCHAPI_API_KEY", ""),
        "cse": os.environ.get("GOOGLE_CSE_API_KEY", "") and os.environ.get("GOOGLE_CSE_ID", ""),
    }.get(provider, "").strip())


def make_config(provider: str):
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


def select_provider(con) -> tuple[str | None, object | None, dict | None]:
    """(name, config, budget_row) — first enabled provider by priority with
    a configured key and remaining budget (unknown quota counts as OK)."""
    for row in roll_cycles(con):
        if not row["enabled"] or not env_key_for(row["provider"]):
            continue
        if row["remaining"] is not None and row["remaining"] <= 0:
            continue
        return row["provider"], make_config(row["provider"]), row
    return None, None, None


def budget_report(con) -> dict:
    rows = roll_cycles(con)
    active, _cfg, _row = select_provider(con)
    return {
        "active_provider": active,
        "providers": [
            {k: (str(v) if isinstance(v, date) else v) for k, v in r.items()}
            for r in rows
        ],
    }


def budget_guard(con, estimated: int) -> tuple[object | None, dict]:
    """Pre-flight: pick provider, warn/clamp against remaining budget.
    Returns (search_config, info). search_config None = refuse."""
    name, cfg, row = select_provider(con)
    if cfg is None:
        rows = roll_cycles(con)
        refills = [r["next_refill"] for r in rows if r["next_refill"]]
        return None, {
            "error": "no search provider has remaining budget",
            "next_refill": min(refills) if refills else None,
            "budget": budget_report(con),
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


# ── Entity context (graph terms for disambiguation) ────────────────

def context_for(con, name: str) -> set[str] | None:
    """Context bag for disambiguation: sector + partner entity names +
    headlines of the cards this entity appears in."""
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


def aliases_for(con, name: str) -> list[str]:
    entity_id = db.find_entity_exact(con, name)
    if entity_id is None:
        return []
    rows = con.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", [entity_id]
    ).fetchall()
    return [r[0] for r in rows]


# ── The sweep itself ───────────────────────────────────────────────

def sweep(con, client: httpx.Client, limit: int) -> dict[str, Any]:
    """Resolve handles for company entities that don't have a decided row
    yet. Budget-guarded, circuit-broken, verify-or-abstain. Counts every
    live query against the provider's registry row."""
    search, binfo = budget_guard(con, limit * 2)
    if search is None:
        return binfo
    remaining = binfo.get("searches_remaining")
    if remaining is not None and remaining < limit * 2:
        limit = max(1, remaining // 2)
    channels = ["linkedin", "x"]
    counts: dict[str, Any] = {"resolved": 0, "abstained": 0, "blocked": 0,
                              "budget": binfo}

    # Count usage. In the resolver service a thread-local dispatcher is
    # already installed (concurrent lookups each bump their own con) — leave
    # it alone and let the caller wire its thread-local. Standalone (prod
    # ingest), install a direct hook for the duration.
    prev_hook = handles.search_usage_hook
    if prev_hook is None:
        handles.search_usage_hook = lambda prov: db.bump_search_usage(con, prov)
    try:
        allow_direct = True   # flips off for the rest of the run on first 999
        for channel in channels:
            todo = db.entities_missing_handles(con, channel, limit)
            consecutive_blocked = 0
            for i, (name, aliases) in enumerate(todo):
                if i:
                    handles.polite_sleep(handles.SEARCH_DELAY_S)
                result = handles.resolve_channel(
                    channel, name, aliases,
                    client=client, search=search, allow_direct=allow_direct,
                    context=context_for(con, name),
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
                        allow_direct = False
                    if result.comment == handles.COMMENT_SEARCH_UNAVAILABLE:
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
        handles.search_usage_hook = prev_hook


def run_sweep(duckdb_path: str, limit: int = 25) -> dict[str, Any]:
    """Self-contained entry point for the prod ingest function: open the
    DB, sweep, close. Never raises — ingest must not fail because handle
    resolution couldn't run."""
    try:
        client = httpx.Client(
            headers={"User-Agent": handles._UA}, timeout=15.0, follow_redirects=True
        )
        con = db.connect(duckdb_path)
        try:
            db.init_schema(con)
            return sweep(con, client, limit)
        finally:
            con.close()
            client.close()
    except Exception as exc:  # noqa: BLE001
        log.exception("handle sweep failed (non-fatal)")
        return {"error": str(exc)}
