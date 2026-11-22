"""Entity heat — how much is actually happening around a company (2026-11-21).

v1 summed the confidence of every edge touching a node. That measured how
often a company is written about, not what happens to it: Figure's 771 edges
came from 394 cards, the same "NVIDIA invests in Figure" was re-emitted on 21
cards, and 57% of its heat was "competes_with" (mostly inferred/speculative).

v2, used by the export (graph doc → posters) and mirrored in
frontend/assets/graph.js `computeHeat` (keep the two identical):

  * one relationship restated within REPEAT_DAYS is ONE event: the same pair +
    type seen again later than that is new news and counts again;
  * weight = confidence × EVIDENCE_WEIGHT[evidence_type] (hard evidence counts
    more than a guess);
  * rivalry (competes_with, benchmarks_against) is context, not momentum: it
    gets no heat (it still draws as an edge);
  * invalidated edges and self-loops get no heat;
  * normalized 0..1 against the hottest node in the SAME edge set, so a 30-day
    view ranks companies on those 30 days, then raised to CURVE for display.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

EVIDENCE_WEIGHT = {"direct": 1.0, "web_grounded": 0.85, "inferred": 0.6, "speculative": 0.3}
DEFAULT_EVIDENCE_WEIGHT = 0.6
NO_HEAT_RELS = frozenset({"competes_with", "benchmarks_against"})
# Same as graph.js SYMMETRIC_RELS / relationship_vocab.yaml directional: false.
SYMMETRIC_RELS = frozenset({"partners_with", "competes_with", "benchmarks_against"})
REPEAT_DAYS = 90
# Display curve: v1 summed every repeat, so its colors ran hotter; (raw/top) ** 0.7 keeps the
# v2 ranking but brings the map's brightness back near v1's (top-20 ~0.15–0.4 again, 2026-11-21).
CURVE = 0.7


def _day(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def edge_weight(e: dict[str, Any]) -> float:
    """Heat one edge can contribute (before repeat collapsing). 0 = none."""
    a, b, rel = e.get("from"), e.get("to"), e.get("rel")
    if not a or not b or a == b or rel in NO_HEAT_RELS or e.get("status") == "invalidated":
        return 0.0
    conf = float(e.get("confidence") or 0.0)
    return conf * EVIDENCE_WEIGHT.get(e.get("evidence_type") or "", DEFAULT_EVIDENCE_WEIGHT)


def compute(edges: Iterable[dict[str, Any]]) -> dict[str, float]:
    """name → heat in 0..1 over these edges (dicts with from/to/rel/confidence/
    evidence_type/status/catalyst_date). Names with no heat-bearing edge are absent."""
    rows = []
    for e in edges:
        w = edge_weight(e)
        if w <= 0:
            continue
        a, b, rel = e["from"], e["to"], e["rel"]
        if rel in SYMMETRIC_RELS and b < a:
            a, b = b, a
        rows.append((_day(e.get("catalyst_date")) or date.min, (a, rel, b), w))
    rows.sort(key=lambda r: r[0])

    events: dict[tuple[str, str, str], list[list[Any]]] = {}   # key → [[start_day, weight], …]
    for d, key, w in rows:
        evs = events.setdefault(key, [])
        last = evs[-1] if evs else None
        if last is not None and d != date.min and last[0] != date.min and (d - last[0]).days < REPEAT_DAYS:
            last[1] = max(last[1], w)                          # same event, restated
        elif last is not None and (d == date.min or last[0] == date.min):
            last[1] = max(last[1], w)                          # undated: can't tell → same event
        else:
            evs.append([d, w])

    raw: dict[str, float] = {}
    for (a, _rel, b), evs in events.items():
        total = sum(w for _d, w in evs)
        raw[a] = raw.get(a, 0.0) + total
        raw[b] = raw.get(b, 0.0) + total
    top = max(raw.values(), default=0.0)
    return {n: round((h / top) ** CURVE, 3) for n, h in raw.items()} if top else {}
