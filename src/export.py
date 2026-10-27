"""Frontend export — writes data/exports/cards.json (schema_version: 1).

Contract with the graph UI is documented in docs/technical_spec.md §2.5.
Kept framework-free: plain dicts → json.dumps.

The payload surfaces the same catalysts-as-cards view PLUS a flat
`graph` projection (nodes + edges) pre-filtered by
`cfg.graph.{time_window_days, min_edge_confidence, show_invalidated}`
so the frontend can render the graph without re-querying DuckDB. Edge
tooltips use the `mechanism` field; colored-status overlays key off
`status` + `confidence`. See spec §2.5 / §2.6.

Graph-level insights (`graph_insights[]`) are computed by src/detect.py
(§2.9a) — comparative, rate-normalised claims like "3.2x prior quarter".
Always a list: detectors that find nothing (or fail) yield [], so the
frontend and soljet-postiz never special-case a missing key.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import duckdb

from . import db, detect, handles, recap, story
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
               significance_score, research_sources, hashtags
        FROM catalysts
        WHERE {' AND '.join(where_clauses)}
        ORDER BY timestamp DESC, entry_id DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    aliases = story.load_aliases(con)   # once per export, for in_story tagging
    cards = []
    for row in catalyst_rows:
        (
            catalyst_id, entry_id, ts, source_url, raw_finding,
            headline, sentiment, sent_takeaways,
            significance_score, research_sources, hashtags_json,
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
        # in_story: named in THIS card's own news text (src/story.py). Not a
        # truth signal — background entities come from the extractor's
        # research and are shown dimmed on the card, in full on the graph.
        story_text = f"{headline}\n{raw_finding}"
        entities = [
            {"name": n, "ticker": t, "type": ty,
             "in_story": story.mentioned(n, story_text, aliases)}
            for (n, t, ty) in ent_rows
        ]
        in_story = {e["name"] for e in entities if e["in_story"]}

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
              AND r.entity_a_id <> r.entity_b_id   -- self-loops are extraction noise
            ORDER BY r.confidence DESC, r.rel_type, ea.name, eb.name
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
                "in_story": a in in_story or b in in_story,
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
            # False = market/sector news naming no company: every link on the
            # card is background research (UI shows a hint instead of a story).
            "story_names_entities": bool(in_story),
            "sentiment": sentiment,
            "confidence": round(top_conf, 2),
            "significance": (
                round(float(significance_score), 2) if significance_score is not None else None
            ),
            "research_sources": (
                json.loads(research_sources) if research_sources else []
            ),
            "source_url": source_url,
            # Priority-ordered (entity, topic, theme, sector); consumers take
            # a prefix to fit their budget. See src/hashtags.py.
            "hashtags": json.loads(hashtags_json) if hashtags_json else [],
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


def _precompute_layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Bake deterministic x/y layout coordinates into each node, in place.

    Fruchterman-Reingold, pure python, seeded by node name — so the
    frontend can render with cytoscape's zero-cost `preset` layout instead
    of running cose-bilkent on the main thread (seconds of frozen UI at
    ~500 nodes). Runs offline at export time where the cost is invisible.
    Iterations are capped so the whole pass stays a few seconds.
    """
    import math
    import random

    n = len(nodes)
    if n == 0:
        return
    rng = random.Random("ckg-layout-v1")  # fixed seed → stable positions run-to-run
    area_side = 100.0 * math.sqrt(n)
    k = area_side / math.sqrt(n)  # ideal spring length
    idx = {nd["id"]: i for i, nd in enumerate(nodes)}
    pos = [[rng.uniform(0, area_side), rng.uniform(0, area_side)] for _ in range(n)]
    pairs = [(idx[e["from"]], idx[e["to"]]) for e in edges
             if e["from"] in idx and e["to"] in idx]

    iters = 50 if n <= 600 else 30
    temp = area_side / 10.0
    cool = temp / (iters + 1)
    for _ in range(iters):
        disp = [[0.0, 0.0] for _ in range(n)]
        # repulsion (O(n^2) — fine at export time for a few hundred nodes)
        for i in range(n):
            xi, yi = pos[i]
            di = disp[i]
            for j in range(i + 1, n):
                dx = xi - pos[j][0]
                dy = yi - pos[j][1]
                d2 = dx * dx + dy * dy or 0.01
                f = (k * k) / d2
                fx, fy = dx * f, dy * f
                di[0] += fx; di[1] += fy
                disp[j][0] -= fx; disp[j][1] -= fy
        # attraction along edges
        for i, j in pairs:
            dx = pos[i][0] - pos[j][0]
            dy = pos[i][1] - pos[j][1]
            d = math.sqrt(dx * dx + dy * dy) or 0.1
            f = (d * d) / k / d
            fx, fy = dx * f, dy * f
            disp[i][0] -= fx; disp[i][1] -= fy
            disp[j][0] += fx; disp[j][1] += fy
        for i in range(n):
            dx, dy = disp[i]
            d = math.sqrt(dx * dx + dy * dy) or 0.1
            step = min(d, temp)
            pos[i][0] += dx / d * step
            pos[i][1] += dy / d * step
        temp -= cool

    for i, nd in enumerate(nodes):
        nd["x"] = round(pos[i][0], 1)
        nd["y"] = round(pos[i][1], 1)


_LAYOUT_DDL = """
CREATE TABLE IF NOT EXISTS graph_layout (
    sector     VARCHAR NOT NULL,
    name       VARCHAR NOT NULL,
    x          DOUBLE  NOT NULL,
    y          DOUBLE  NOT NULL,
    updated_at TIMESTAMP DEFAULT now(),
    PRIMARY KEY (sector, name)
)
"""


def _name_seed_xy(name: str, side: float) -> tuple[float, float]:
    """Deterministic start position from the NAME alone (independent of how
    many other nodes exist or where the name sorts)."""
    import hashlib

    h = hashlib.sha256(name.encode()).digest()
    return (int.from_bytes(h[:4], "big") / 2**32 * side,
            int.from_bytes(h[4:8], "big") / 2**32 * side)


def full_history_layout(con: duckdb.DuckDBPyConnection, sector: str,
                        iters: int = 60, block: int = 256) -> dict[str, list[float]]:
    """x/y for EVERY entity in any live relationship of the sector — the
    Graph tab's full history (built client-side from card docs; see frontend
    buildFullGraph). Sourced from DuckDB, NOT the exported cards, so it never
    depends on export.card_limit.

    Stable across days: positions persist in DuckDB `graph_layout`. Warm runs
    FREEZE every known node; only new nodes move — they start at the mean of
    their already-placed neighbours (+ a small name-hashed offset) and settle
    against the fixed graph. No new nodes → nothing moves, nothing recomputed.
    A cold run (first time, <80% known, or LAYOUT_RESET=true) does the full
    Fruchterman-Reingold anneal. numpy, repulsion in row blocks (O(block·n) memory).
    (A browser force layout at 2k nodes never finished; 2026-10-27.)
    """
    import os

    import numpy as np

    rows = con.execute(
        """
        SELECT DISTINCT ea.name, eb.name
        FROM relationships r
        JOIN catalysts c ON c.catalyst_id = r.catalyst_id
        JOIN entities ea ON ea.entity_id = r.entity_a_id
        JOIN entities eb ON eb.entity_id = r.entity_b_id
        WHERE c.sector = ? AND r.status IN ('active', 'materialized')
          AND r.entity_a_id <> r.entity_b_id
        """,
        [sector],
    ).fetchall()
    order = sorted({x for pair in rows for x in pair})
    names = {nm: i for i, nm in enumerate(order)}
    n = len(names)
    if n == 0:
        return {}
    con.execute(_LAYOUT_DDL)
    reset = os.environ.get("LAYOUT_RESET", "").strip().lower() in ("1", "true", "yes")
    prev = {} if reset else {nm: (x, y) for nm, x, y in con.execute(
        "SELECT name, x, y FROM graph_layout WHERE sector = ?", [sector]).fetchall()}

    k = 100.0
    side = k * np.sqrt(max(1000, -(-n // 1000) * 1000))      # grows in 1k-node steps
    ij = np.array(sorted({(min(names[a], names[b]), max(names[a], names[b])) for a, b in rows}),
                  dtype=np.int64).reshape(-1, 2)
    known = np.array([nm in prev for nm in order])
    warm = known.sum() >= 0.8 * n

    pos = np.array([prev.get(nm) or _name_seed_xy(nm, side) for nm in order], dtype=float)
    if warm:
        new_idx = np.flatnonzero(~known)
        if len(new_idx) == 0:
            # Nothing new: keep positions exactly (identical data → zero movement).
            return {nm: [round(prev[nm][0], 1), round(prev[nm][1], 1)] for nm in order}
        nbrs: dict[int, list[int]] = {}
        for a, b in ij:
            nbrs.setdefault(int(a), []).append(int(b)); nbrs.setdefault(int(b), []).append(int(a))
        for i in new_idx:                                # start next to placed neighbours
            placed = [j for j in nbrs.get(int(i), []) if known[j]]
            if placed:
                jx, jy = _name_seed_xy(order[i], 2 * k)
                pos[i] = pos[placed].mean(0) + (jx - k, jy - k)
        movable, temp = new_idx, k * 2.0
    else:
        movable, temp = np.arange(n), side / 10.0
    cool = temp / (iters + 1)
    for _ in range(iters):
        disp = np.zeros_like(pos)
        for s0 in range(0, len(movable), block):                       # repulsion, blocked
            rows_i = movable[s0:s0 + block]
            delta = pos[rows_i, None, :] - pos[None, :, :]
            d2 = np.maximum((delta ** 2).sum(-1), 0.01)
            disp[rows_i] = (delta * (k * k / d2)[..., None]).sum(1)
        if len(ij):                                                    # attraction
            dv = pos[ij[:, 0]] - pos[ij[:, 1]]
            d = np.maximum(np.sqrt((dv ** 2).sum(-1)), 0.1)
            f = dv * (d / k)[:, None]
            np.add.at(disp, ij[:, 0], -f)
            np.add.at(disp, ij[:, 1], f)
        sub = disp[movable]
        length = np.maximum(np.sqrt((sub ** 2).sum(-1)), 0.1)
        pos[movable] += sub / length[:, None] * np.minimum(length, temp)[:, None]
        temp -= cool

    out = {nm: [round(float(pos[i, 0]), 1), round(float(pos[i, 1]), 1)] for nm, i in names.items()}
    # Bulk write (executemany is row-by-row slow in DuckDB), atomically: if the
    # INSERT fails the DELETE rolls back, so the stored layout survives and the
    # next run stays warm instead of re-laying out (reshuffling) everything.
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM graph_layout WHERE sector = ?", [sector])
        con.execute(
            "INSERT INTO graph_layout (sector, name, x, y) "
            "SELECT ?, unnest(?::VARCHAR[]), unnest(?::DOUBLE[]), unnest(?::DOUBLE[])",
            [sector, list(out), [v[0] for v in out.values()], [v[1] for v in out.values()]],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return out


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

    node_list = sorted(nodes.values(), key=lambda n: n["heat"], reverse=True)
    _precompute_layout(node_list, edges)
    return {
        "nodes": node_list,
        "edges": edges,
    }


# Firestore caps a document at 1 MiB (1,048,576 B). Budget well under it:
# JSON length only approximates Firestore's stored size, and the graph doc
# also carries stats + graph_insights + layout coordinates.
GRAPH_DOC_BUDGET = 900_000


def _graph_bytes(cards: list[dict[str, Any]]) -> int:
    """Approximate serialized size of the graph built from ``cards`` (no layout)."""
    edges = 0
    names: set[str] = set()
    for c in cards:
        for r in c.get("relationships", []):
            edges += len(json.dumps(r, default=str)) + 80   # + catalyst_id/date/keys
        names.update(e["name"] for e in c.get("entities", []))
    return edges + 140 * len(names)                          # node dict incl. x/y


def _fit_graph_budget(
    cards: list[dict[str, Any]], budget: int = GRAPH_DOC_BUDGET,
) -> tuple[list[dict[str, Any]], str | None]:
    """Newest-first prefix of ``cards`` whose graph fits ``budget`` bytes.

    Returns (cards, oldest date kept). Binary search over the prefix length —
    _graph_bytes is cheap; layout runs once afterwards on the result only.
    """
    ordered = sorted(cards, key=lambda c: (str(c.get("date", "")), c.get("card_id", "")),
                     reverse=True)
    if _graph_bytes(ordered) <= budget:
        kept = ordered
    else:
        lo, hi = 0, len(ordered)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _graph_bytes(ordered[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        kept = ordered[:lo]
    return kept, (str(kept[-1].get("date")) if kept else None)


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
        # §2.9a — comparative claims ("3.2x prior quarter"), the field
        # soljet-postiz quotes verbatim. Computed here, inside the same
        # connection, so it sees exactly the data this export ships.
        insights = detect.graph_insights(con, cfg)
        # §2.9b — weekly chart cards for exactly what the digest names,
        # from the same windows as `insights` (same connection, same data).
        recap.tag_insights(insights, cfg.sector)   # per-row `hashtags` for the digest
        # Graph-tab positions for every entity in the DB (independent of card_limit).
        layout = {"positions": full_history_layout(con, cfg.sector)}
        recap_block = recap.build_recap(con, cfg.sector, insights)
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
    # The day window alone doesn't bound the doc: news volume grew and the
    # 90d graph hit 2.2 MiB, so every Firestore write was rejected from
    # 2026-08-18 on (silently — graph froze, cards kept updating). Keep the
    # NEWEST catalysts whose graph fits GRAPH_DOC_BUDGET, oldest dropped first.
    graph_cards, graph_since = _fit_graph_budget(graph_cards)
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
            # Oldest catalyst date actually in the graph after the size cap
            # (may be later than graph_window_days implies).
            "graph_since": graph_since,
        },
        "cards": cards,
        "graph": graph,
        # Positions for the Graph tab's full-history view (built client-side
        # from card docs). Own Firestore doc — never inside the capped graph doc.
        "layout": layout,
        "graph_insights": insights,   # §2.9a detectors (src/detect.py)
        "recap": recap_block,          # §2.9b weekly chart cards (src/recap.py)
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
