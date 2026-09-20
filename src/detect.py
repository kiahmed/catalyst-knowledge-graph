"""Graph insight detectors — populates `graph_insights[]` in the export.

Spec: docs/technical_spec.md §2.9a. Consumer: soljet-postiz, which quotes
`headline` verbatim (it never re-derives a claim from raw numbers), so a
headline here IS published copy — it must be literally true and specific.

The contract each insight satisfies:
  {"type": ..., "entity"|"rel_type": ..., "growth_ratio": 3.2,
   "headline": "NVIDIA: 12 new partnerships in 30 days (3.2x prior quarter)"}

What makes this different from `stats.top_chokepoint_entity` (a coarse
all-time count): every number below is a RECENT window measured against the
entity's OWN trailing baseline, rate-normalised, so the claim is comparative.

No LLM. Pure SQL over the edges already in DuckDB.
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb

log = logging.getLogger(__name__)

# Only 'live' edges count toward a trend — invalidated ones were disproven,
# and counting them would let a retracted story inflate a growth claim.
_LIVE_STATUSES = ("active", "materialized")

# Human phrasing per relationship type, used when a rel_type trend is the
# subject ("12 new partnerships" reads; "12 new partners_with" does not).
_REL_PHRASE: dict[str, str] = {
    "partners_with": "partnerships",
    "integrates_with": "integrations",
    "pilots": "pilot deployments",
    "deploys": "deployments",
    "acquires": "acquisitions",
    "invests_in": "investments",
    "supplies": "supply agreements",
    "competes_with": "competitive collisions",
    "displaces": "displacements",
    "benchmarks_against": "benchmark comparisons",
    "regulates": "regulatory actions",
    "litigates_against": "legal actions",
    "hires_from": "talent moves",
    "spins_out_from": "spinouts",
    "built_on": "technical dependencies",
}


def _phrase(rel_type: str, count: int) -> str:
    word = _REL_PHRASE.get(rel_type, rel_type.replace("_", " "))
    if count == 1 and word.endswith("s"):
        word = word[:-1]
    return word


def _fmt_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "no prior activity"
    return f"{ratio:.1f}x"


def entity_velocity(
    con: duckdb.DuckDBPyConnection,
    sector: str,
    window_days: int,
    baseline_days: int,
    min_recent: int,
    min_growth_ratio: float,
    top_n: int,
    min_baseline: int = 3,
) -> list[dict[str, Any]]:
    """Entities whose edge formation accelerated vs their own trailing rate.

    growth_ratio compares RATES (edges/day), not raw counts, so a 30-day
    window is comparable to a 90-day baseline.

    A multiplier is only claimed when the baseline has at least
    `min_baseline` edges. Without that floor a single prior edge yields
    "150x prior quarter" (seen on live data 2026-09-20) — arithmetically
    true, editorially indefensible. Thin-baseline entities still surface,
    but stated as raw counts ("50 in 30 days, vs 1 in the prior 90").
    """
    rows = con.execute(
        f"""
        WITH edges AS (
            SELECT e.name AS entity, r.rel_type, c.timestamp AS ts
            FROM relationships r
            JOIN catalysts c ON c.catalyst_id = r.catalyst_id
            JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
            WHERE c.sector = ?
              AND r.status IN {_LIVE_STATUSES}
        ),
        recent AS (
            SELECT entity, COUNT(*) AS n
            FROM edges
            WHERE ts >= (CURRENT_DATE - INTERVAL {int(window_days)} DAY)
            GROUP BY entity
        ),
        baseline AS (
            SELECT entity, COUNT(*) AS n
            FROM edges
            WHERE ts <  (CURRENT_DATE - INTERVAL {int(window_days)} DAY)
              AND ts >= (CURRENT_DATE - INTERVAL {int(window_days + baseline_days)} DAY)
            GROUP BY entity
        )
        SELECT recent.entity, recent.n, COALESCE(baseline.n, 0)
        FROM recent LEFT JOIN baseline USING (entity)
        WHERE recent.n >= ?
        """,
        [sector, min_recent],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for entity, recent_n, base_n in rows:
        recent_rate = recent_n / window_days
        base_rate = (base_n / baseline_days) if base_n else 0.0
        # Only a real baseline earns a multiplier (see docstring).
        ratio = (recent_rate / base_rate) if base_n >= min_baseline else None
        if ratio is None:
            # Thin/absent baseline: newsworthy only if the burst is substantial,
            # and reported as counts rather than an inflated multiplier.
            if recent_n < min_recent * 2:
                continue
        elif ratio < min_growth_ratio:
            continue
        top_rel = con.execute(
            f"""
            SELECT r.rel_type, COUNT(*) AS n
            FROM relationships r
            JOIN catalysts c ON c.catalyst_id = r.catalyst_id
            JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
            WHERE c.sector = ? AND e.name = ?
              AND r.status IN {_LIVE_STATUSES}
              AND c.timestamp >= (CURRENT_DATE - INTERVAL {int(window_days)} DAY)
            GROUP BY r.rel_type ORDER BY n DESC, r.rel_type LIMIT 1
            """,
            [sector, entity],
        ).fetchone()
        rel_word = _phrase(top_rel[0], recent_n) if top_rel else "new edges"
        if ratio is None:
            prior = (f"vs {base_n} in the prior {baseline_days}" if base_n
                     else "first activity on record")
            headline = (f"{entity}: {recent_n} {rel_word} in {window_days} days "
                        f"({prior})")
        else:
            headline = (f"{entity}: {recent_n} {rel_word} in {window_days} days "
                        f"({_fmt_ratio(ratio)} prior {baseline_days}-day rate)")
        out.append({
            "type": "chokepoint",
            "entity": entity,
            "window_days": window_days,
            "baseline_days": baseline_days,
            "recent_count": recent_n,
            "baseline_count": base_n,
            "growth_ratio": round(ratio, 2) if ratio is not None else None,
            "headline": headline,
        })

    # Rank by ratio, but put no-baseline newcomers after real accelerations —
    # "3.2x prior quarter" is a stronger claim than "first activity".
    out.sort(key=lambda i: (i["growth_ratio"] is not None, i["growth_ratio"] or 0,
                            i["recent_count"]), reverse=True)
    return out[:top_n]


def relationship_velocity(
    con: duckdb.DuckDBPyConnection,
    sector: str,
    window_days: int,
    baseline_days: int,
    min_recent: int,
    min_growth_ratio: float,
    top_n: int,
    min_baseline: int = 3,
) -> list[dict[str, Any]]:
    """Relationship TYPES accelerating across the sector — the 'what kind of
    thing is happening now' claim (consolidation wave, pilot wave, ...)."""
    rows = con.execute(
        f"""
        WITH edges AS (
            SELECT r.rel_type, c.timestamp AS ts
            FROM relationships r
            JOIN catalysts c ON c.catalyst_id = r.catalyst_id
            WHERE c.sector = ? AND r.status IN {_LIVE_STATUSES}
        ),
        recent AS (
            SELECT rel_type, COUNT(*) AS n FROM edges
            WHERE ts >= (CURRENT_DATE - INTERVAL {int(window_days)} DAY)
            GROUP BY rel_type
        ),
        baseline AS (
            SELECT rel_type, COUNT(*) AS n FROM edges
            WHERE ts <  (CURRENT_DATE - INTERVAL {int(window_days)} DAY)
              AND ts >= (CURRENT_DATE - INTERVAL {int(window_days + baseline_days)} DAY)
            GROUP BY rel_type
        )
        SELECT recent.rel_type, recent.n, COALESCE(baseline.n, 0)
        FROM recent LEFT JOIN baseline USING (rel_type)
        WHERE recent.n >= ?
        """,
        [sector, min_recent],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for rel_type, recent_n, base_n in rows:
        if base_n < min_baseline:
            continue                      # thin baseline => no honest multiplier
        base_rate = base_n / baseline_days
        ratio = (recent_n / window_days) / base_rate
        if ratio < min_growth_ratio:
            continue
        out.append({
            "type": "velocity",
            "rel_type": rel_type,
            "window_days": window_days,
            "baseline_days": baseline_days,
            "recent_count": recent_n,
            "baseline_count": base_n,
            "growth_ratio": round(ratio, 2),
            "headline": (f"{_phrase(rel_type, recent_n).capitalize()} across the sector: "
                         f"{recent_n} in {window_days} days "
                         f"({_fmt_ratio(ratio)} prior {baseline_days}-day rate)"),
        })
    out.sort(key=lambda i: (i["growth_ratio"], i["recent_count"]), reverse=True)
    return out[:top_n]


def graph_insights(con: duckdb.DuckDBPyConnection, cfg) -> list[dict[str, Any]]:
    """All detectors, ordered strongest-claim-first. Never raises: a broken
    detector must not cost the export (cards are the product; insights are
    a bonus lane)."""
    ins = cfg.insights
    if not ins.enabled:
        return []
    try:
        out = entity_velocity(
            con, cfg.sector, ins.window_days, ins.baseline_days,
            ins.min_recent, ins.min_growth_ratio, ins.top_n_entities,
            ins.min_baseline,
        ) + relationship_velocity(
            con, cfg.sector, ins.window_days, ins.baseline_days,
            ins.min_recent_rel, ins.min_growth_ratio, ins.top_n_rel_types,
            ins.min_baseline,
        )
        log.info("graph_insights computed n=%d", len(out))
        return out
    except Exception as exc:  # noqa: BLE001
        log.exception("graph_insights failed (non-fatal): %s", exc)
        return []
