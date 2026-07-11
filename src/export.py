"""Frontend export — writes data/exports/cards.json (schema_version: 1).

Contract with the graph UI is documented in docs/technical_spec.md §2.5.
Kept framework-free: plain dicts → json.dumps.

The payload surfaces the same catalysts-as-cards view PLUS a flat
`graph` projection (nodes + edges) pre-filtered by
`cfg.graph.{time_window_days, min_edge_confidence, show_invalidated}`
so the frontend can render the graph without re-querying DuckDB. Edge
tooltips use the `mechanism` field; colored-status overlays key off
`status` + `confidence`. See spec §2.5 / §2.6.

Graph-level insights (chokepoint/velocity/cluster-break) remain TODO
until the detector tickets — emitted as an empty list so the frontend
can render the overlay panel without special-casing a missing key.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import duckdb

from . import db, handles
from .config import RoboticsConfig


def _card_type_from_rels(rels: list[dict[str, Any]]) -> str:
    """Pick the card_type from the highest-confidence relationship. Falls back to 'catalyst'."""
    if not rels:
        return "catalyst"
    top = max(rels, key=lambda r: r["confidence"])
    # Rough bucket: partnership / deal / competitive / regulatory / talent / technical
    buckets = {
        "partners_with": "partnership",
        "integrates_with": "partnership",
        "pilots": "partnership",
        "deploys": "partnership",
        "acquires": "deal",
        "invests_in": "deal",
        "supplies": "deal",
        "competes_with": "competitive",
        "displaces": "competitive",
        "benchmarks_against": "competitive",
        "regulates": "regulatory",
        "litigates_against": "regulatory",
        "hires_from": "talent",
        "spins_out_from": "talent",
        "built_on": "technical",
    }
    return buckets.get(top["rel"], "catalyst")


def _short_subtitle(sentiment_takeaways: str | None) -> str:
    """Take the first 'Direct:' or 'Market Dynamics:' line, trim to ~100 chars."""
    if not sentiment_takeaways:
        return ""
    for line in sentiment_takeaways.splitlines():
        line = line.strip()
        if line.lower().startswith(("direct:", "market dynamics:", "indirect:")):
            text = line.split(":", 1)[1].strip() if ":" in line else line
            return (text[:120] + "…") if len(text) > 120 else text
    return ""


def _twitter_text(headline: str, entities: list[dict[str, Any]]) -> str:
    """Simple template: headline + $TICKERs for public companies."""
    tickers = [f"${e['ticker']}" for e in entities if e.get("ticker")]
    tag_block = " " + " ".join(tickers[:3]) if tickers else ""
    # Twitter cap is 280; reserve ~25 for URL.
    body = headline
    if len(body) + len(tag_block) > 250:
        body = body[:250 - len(tag_block) - 1] + "…"
    return f"{body}{tag_block}".strip()


def _linkedin_text(headline: str, subtitle: str, source_url: str | None) -> str:
    parts = [headline]
    if subtitle:
        parts.append("")
        parts.append(subtitle)
    if source_url:
        parts.append("")
        parts.append(source_url)
    return "\n".join(parts)


def _load_cards(
    con: duckdb.DuckDBPyConnection,
    sector: str,
    limit: int,
    time_window_days: int,
    min_edge_confidence: float,
    show_invalidated: bool,
) -> list[dict[str, Any]]:
    # Time-window filter (0 = no cap).
    where_clauses = ["sector = ?"]
    params: list[Any] = [sector]
    if time_window_days and time_window_days > 0:
        where_clauses.append(f"timestamp >= (CURRENT_DATE - INTERVAL {int(time_window_days)} DAY)")

    catalyst_rows = con.execute(
        f"""
        SELECT catalyst_id, entry_id, timestamp, source_url, raw_finding,
               headline, sentiment_label, sentiment_takeaways,
               significance_score, research_sources
        FROM catalysts
        WHERE {' AND '.join(where_clauses)}
        ORDER BY timestamp DESC, entry_id DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    cards = []
    for row in catalyst_rows:
        (
            catalyst_id, entry_id, ts, source_url, raw_finding,
            headline, sentiment, sent_takeaways,
            significance_score, research_sources,
        ) = row

        # Entities that appear in this catalyst's relationships.
        ent_rows = con.execute(
            """
            SELECT DISTINCT e.name, e.ticker, e.type
            FROM relationships r
            JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
            WHERE r.catalyst_id = ?
            ORDER BY e.name
            """,
            [catalyst_id],
        ).fetchall()
        entities = [
            {"name": n, "ticker": t, "type": ty}
            for (n, t, ty) in ent_rows
        ]

        # Verified social handles from the shared entity_handles cache
        # (contract A of docs/handle-resolution-spec.md). None = abstained —
        # soljet-postiz appends no tag for that entity/channel.
        handle_rows = db.get_handles(
            con, [handles.name_key(e["name"]) for e in entities]
        )
        for e in entities:
            key = handles.name_key(e["name"])
            for channel, field in (("linkedin", "linkedin_handle"), ("x", "x_handle")):
                hit = handle_rows.get((key, channel))
                # Review-flagged (low-confidence) handles stay off cards.
                e[field] = handles.usable_handle(hit[0], hit[2]) if hit else None

        # Edge filters: min_edge_confidence + status visibility.
        status_filter = (
            "r.status IN ('active', 'materialized', 'invalidated')"
            if show_invalidated else
            "r.status IN ('active', 'materialized')"
        )
        rel_rows = con.execute(
            f"""
            SELECT ea.name, r.rel_type, eb.name, r.confidence,
                   r.evidence_type, r.mechanism, r.mechanism_strength,
                   r.impact_magnitude, r.flagged, r.source_refs, r.status
            FROM relationships r
            JOIN entities ea ON ea.entity_id = r.entity_a_id
            JOIN entities eb ON eb.entity_id = r.entity_b_id
            WHERE r.catalyst_id = ?
              AND r.confidence >= ?
              AND {status_filter}
            ORDER BY r.confidence DESC
            """,
            [catalyst_id, min_edge_confidence],
        ).fetchall()
        rels = [
            {
                "from": a, "rel": rt, "to": b,
                "confidence": float(c),
                "evidence_type": et,
                "mechanism": mech,
                "mechanism_strength": float(ms) if ms is not None else None,
                "impact_magnitude": float(im) if im is not None else None,
                "flagged": bool(fl),
                "source_refs": json.loads(sr) if sr else [],
                "status": st or "active",
            }
            for (a, rt, b, c, et, mech, ms, im, fl, sr, st) in rel_rows
        ]

        subtitle = _short_subtitle(sent_takeaways)
        top_conf = rels[0]["confidence"] if rels else 0.0

        cards.append({
            "card_id": entry_id,
            "card_type": _card_type_from_rels(rels),
            "date": ts.isoformat() if ts else None,
            "headline": headline,
            "subtitle": subtitle,
            "entities": entities,
            "relationships": rels,
            "sentiment": sentiment,
            "confidence": round(top_conf, 2),
            "significance": (
                round(float(significance_score), 2) if significance_score is not None else None
            ),
            "research_sources": (
                json.loads(research_sources) if research_sources else []
            ),
            "source_url": source_url,
            "share": {
                "twitter_text": _twitter_text(headline, entities),
                "linkedin_text": _linkedin_text(headline, subtitle, source_url),
            },
        })

    return cards


def _compute_stats(
    con: duckdb.DuckDBPyConnection, sector: str
) -> dict[str, Any]:
    total = con.execute(
        "SELECT COUNT(*) FROM catalysts WHERE sector = ?", [sector]
    ).fetchone()[0]

    last_7d = con.execute(
        """SELECT COUNT(*) FROM catalysts
           WHERE sector = ? AND timestamp >= (CURRENT_DATE - INTERVAL 7 DAY)""",
        [sector],
    ).fetchone()[0]

    # Top chokepoint: entity appearing in the most relationships.
    top_choke_row = con.execute(
        """
        SELECT e.name, COUNT(*) AS cnt
        FROM relationships r
        JOIN catalysts c ON c.catalyst_id = r.catalyst_id
        JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
        WHERE c.sector = ?
        GROUP BY e.name
        ORDER BY cnt DESC
        LIMIT 1
        """,
        [sector],
    ).fetchone()
    top_choke = top_choke_row[0] if top_choke_row else None

    fastest_rel_row = con.execute(
        """
        SELECT r.rel_type, COUNT(*) AS cnt
        FROM relationships r
        JOIN catalysts c ON c.catalyst_id = r.catalyst_id
        WHERE c.sector = ? AND c.timestamp >= (CURRENT_DATE - INTERVAL 14 DAY)
        GROUP BY r.rel_type
        ORDER BY cnt DESC
        LIMIT 1
        """,
        [sector],
    ).fetchone()
    fastest_rel = fastest_rel_row[0] if fastest_rel_row else None

    return {
        "total_catalysts": int(total),
        "catalysts_last_7d": int(last_7d),
        "top_chokepoint_entity": top_choke,
        "fastest_accelerating_relationship": fastest_rel,
    }


def _build_graph_projection(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten cards into a node-and-edge graph suitable for direct UI render.

    Each node is an entity keyed by name; heat = sum of incident edge
    confidences (invalidated edges already excluded by _load_cards). Each
    edge carries catalyst context + mechanism so tooltips can render.
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    for card in cards:
        card_id = card.get("card_id")
        card_date = card.get("date")
        for e in card.get("entities", []):
            name = e["name"]
            if name not in nodes:
                nodes[name] = {
                    "id": name,
                    "name": name,
                    "ticker": e.get("ticker"),
                    "type": e.get("type"),
                    "heat": 0.0,
                    "edge_count": 0,
                }
        for r in card.get("relationships", []):
            a, b = r["from"], r["to"]
            conf = float(r.get("confidence") or 0.0)
            for n in (a, b):
                if n in nodes:
                    nodes[n]["heat"] += conf
                    nodes[n]["edge_count"] += 1
            edges.append({
                "from": a,
                "to": b,
                "rel": r["rel"],
                "confidence": conf,
                "evidence_type": r.get("evidence_type"),
                "mechanism": r.get("mechanism"),
                "status": r.get("status", "active"),
                "flagged": r.get("flagged", False),
                "source_refs": r.get("source_refs", []),
                "catalyst_id": card_id,
                "catalyst_date": card_date,
            })

    # Normalize heat to 0..1 so the UI can color consistently.
    if nodes:
        max_heat = max(n["heat"] for n in nodes.values()) or 1.0
        for n in nodes.values():
            n["heat"] = round(n["heat"] / max_heat, 3)

    return {
        "nodes": sorted(nodes.values(), key=lambda n: n["heat"], reverse=True),
        "edges": edges,
    }


def build_payload(cfg: RoboticsConfig) -> dict[str, Any]:
    """Build the canonical export payload (cards + graph + stats).

    Same shape that gets written to cards.json AND pushed to Firestore.
    Factored out so the two destinations share one source of truth.
    """
    con = db.connect(cfg.duckdb_path)
    try:
        cards = _load_cards(
            con,
            cfg.sector,
            cfg.export.card_limit,
            time_window_days=cfg.graph.time_window_days,
            min_edge_confidence=cfg.graph.min_edge_confidence,
            show_invalidated=cfg.graph.show_invalidated,
        )
        stats = _compute_stats(con, cfg.sector)
    finally:
        con.close()

    # Graph projection is windowed independently of the card list: the graph
    # lands in ONE Firestore doc (CKG-<sector>/graph/sectors/<sector>) which
    # hard-caps at 1 MiB — all-time nodes+edges blew it at 248 cards
    # (2026-07-06). Cards sync in full; the graph doc stays the recent view.
    if cfg.graph.graph_window_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg.graph.graph_window_days)).strftime("%Y-%m-%d")
        graph_cards = [c for c in cards if str(c.get("date", "")) >= cutoff]
    else:
        graph_cards = cards
    graph = _build_graph_projection(graph_cards)

    return {
        "schema_version": cfg.export.schema_version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "sector": cfg.sector,
        "stats": stats,
        "graph_config": {
            "time_window_days": cfg.graph.time_window_days,
            "min_edge_confidence": cfg.graph.min_edge_confidence,
            "show_invalidated": cfg.graph.show_invalidated,
            "graph_window_days": cfg.graph.graph_window_days,
        },
        "cards": cards,
        "graph": graph,
        "graph_insights": [],  # populated when detectors land (Phase 1 W4)
    }


def export_cards(cfg: RoboticsConfig) -> dict[str, Any]:
    """Render cards.json to cfg.export.cards_json_path. Returns summary dict."""
    payload = build_payload(cfg)

    out_path = cfg.export.cards_json_path
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, out_path)

    return {
        "path": out_path,
        "cards": len(payload["cards"]),
        "nodes": len(payload["graph"]["nodes"]),
        "edges": len(payload["graph"]["edges"]),
        "stats": payload["stats"],
    }
