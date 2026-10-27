"""Weekly recap cards — spec §2.9b (charts for what the digest names).

Turns the week's top graph_insights[] into a few simple, noob-readable chart
cards: one question, one big number, one plain sentence, one chart. Three kinds:

  mix            per named entity — what its surge is made of, vs before
  grounding      per named entity — how much is reported news vs model inference
  concentration  the named sector trend — who the sector leans on, vs before

Scope is a hard limit: only the entities/trend the digest names (top 2
chokepoint insights + top velocity insight), same window/baseline as their
headlines, no backfill. Every number here comes from the same DuckDB window
queries as src/detect.py, so a chart can never disagree with its headline.

Deterministic, no LLM. `cards` is ordered: cards[0] is the one shown publicly
/ posted this week; the rest are the full set (future paid-tier unlocks).
"""
from __future__ import annotations

from datetime import date
from typing import Any

import duckdb

from . import hashtags
from .detect import _LIVE_STATUSES, _phrase

# Relationship types → 5 meaning groups (+ Other). Colours live in the
# template, keyed by group name, so the same group is the same colour on
# every card.
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Competition", ("competes_with", "displaces", "benchmarks_against", "litigates_against")),
    ("Partnership", ("partners_with", "integrates_with")),
    ("Deployment", ("deploys", "pilots")),
    ("Supply & tech", ("supplies", "built_on")),
    ("Capital & talent", ("invests_in", "acquires", "spins_out_from", "hires_from")),
]
_GROUP_OF = {rel: g for g, rels in GROUPS for rel in rels}
_GROUP_WORD = {  # how a group reads mid-sentence
    "Competition": "rivalry", "Partnership": "partnerships", "Deployment": "deployments",
    "Supply & tech": "supply and tech ties", "Capital & talent": "money and talent moves",
    "Other": "other links",
}

# Which endpoint a concentration chart counts, per relationship type:
# "A built_on B" → the platform is B; "A supplies B" → the supplier is A.
_COUNT_SIDE = {
    "built_on": "b", "deploys": "b", "pilots": "b", "displaces": "b",
    "supplies": "a", "invests_in": "a", "acquires": "a", "regulates": "a",
    "hires_from": "b", "spins_out_from": "b", "benchmarks_against": "b",
    "litigates_against": "b",
}  # anything else (partners_with, integrates_with, competes_with) → both ends
_CONC_TITLE = {
    "built_on": "Who does the sector build on?",
    "supplies": "Who supplies the sector?",
    "deploys": "What is the sector deploying?",
    "integrates_with": "Who is everyone integrating with?",
    "partners_with": "Who is everyone partnering with?",
    "competes_with": "Who is everyone competing with?",
    "invests_in": "Who is investing in the sector?",
}
TOP_K = 5

# Verdict = the "so what", ≤10 words, plain English, no investment framing
# (never "valuable", "buy", "bullish"). Keyed by the dominant activity group.
_MIX_VERDICT = {
    "Competition": "{e} is now a target: rivals are lining up.",
    "Partnership": "{e} is becoming a hub others want to join.",
    "Deployment": "{e}'s products are moving into real-world use.",
    "Supply & tech": "{e} is turning into infrastructure others rely on.",
    "Capital & talent": "Money and talent are flowing toward {e}.",
    "Other": "{e} is suddenly getting far more attention.",
}


def _group(rel: str) -> str:
    return _GROUP_OF.get(rel, "Other")


# Topic hashtag per activity group (same vocabulary as card hashtags).
_GROUP_TAG = {
    "Competition": "#Competition", "Partnership": "#Partnership",
    "Deployment": "#Deployment", "Supply & tech": "#SupplyChain",
    "Capital & talent": "#Funding",
}


def _tags(*candidates: str | None) -> list[str]:
    """Dedup, drop empties, cap at hashtags.MAX_TAGS, keep priority order."""
    out: list[str] = []
    for t in candidates:
        if t and t not in out:
            out.append(t)
    return out[:hashtags.MAX_TAGS]


def _sector_tag(sector: str) -> str | None:
    return hashtags.SECTOR_TAG.get((sector or "").strip().lower())


def insight_hashtags(ins: dict[str, Any], sector: str) -> list[str]:
    """Hashtags for one graph_insights[] row: entity (or none for a sector
    trend), its leading topic, sector floor. Consumers (soljet-postiz digest)
    union the rows they actually post instead of guessing."""
    if ins.get("type") == "chokepoint":
        topic = hashtags.TOPIC_TAG.get(ins.get("top_rel") or "")
        return _tags(hashtags.camel_tag(ins.get("entity", "")), topic, _sector_tag(sector))
    return _tags(hashtags.TOPIC_TAG.get(ins.get("rel_type") or ""), _sector_tag(sector))


def tag_insights(insights: list[dict[str, Any]], sector: str) -> list[dict[str, Any]]:
    """Add `hashtags` to every insight row in place (additive field)."""
    for ins in insights:
        ins["hashtags"] = insight_hashtags(ins, sector)
    return insights


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


def _windows(window_days: int, baseline_days: int) -> tuple[str, str]:
    # Identical predicates to src/detect.py (CURRENT_DATE-anchored).
    recent = f"c.timestamp >= (CURRENT_DATE - INTERVAL {int(window_days)} DAY)"
    base = (f"c.timestamp <  (CURRENT_DATE - INTERVAL {int(window_days)} DAY) AND "
            f"c.timestamp >= (CURRENT_DATE - INTERVAL {int(window_days + baseline_days)} DAY)")
    return recent, base


def _entity_counts(con, sector: str, entity: str, where: str) -> list[tuple[str, str, int]]:
    return con.execute(
        f"""
        SELECT r.rel_type, r.evidence_type, COUNT(*)
        FROM relationships r
        JOIN catalysts c ON c.catalyst_id = r.catalyst_id
        JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
        WHERE c.sector = ? AND e.name = ? AND r.status IN {_LIVE_STATUSES} AND {where}
        GROUP BY 1, 2
        """,
        [sector, entity],
    ).fetchall()


def _poss(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _per_month(n: int, days: int) -> str:
    if n == 0:
        return "none"
    rate = n * 30 / days
    return f"about {round(rate)}" if rate >= 1 else "less than 1"


def _mix_card(ins: dict, now_rows, prior_rows, sector: str = "") -> dict[str, Any]:
    entity = ins["entity"]
    now: dict[str, int] = {}
    before: dict[str, int] = {}
    for rel, _ev, n in now_rows:
        now[_group(rel)] = now.get(_group(rel), 0) + n
    for rel, _ev, n in prior_rows:
        before[_group(rel)] = before.get(_group(rel), 0) + n
    total, prior_total = sum(now.values()), sum(before.values())
    lead = max(now, key=lambda g: (now[g], g)) if now else "Other"
    share = now.get(lead, 0) / total if total else 0
    lead_txt = (f"Mostly {_GROUP_WORD[lead]}: {now[lead]} of {total}."
                if share >= 0.5 else
                f"Biggest share is {_GROUP_WORD[lead]}: {now.get(lead, 0)} of {total}.")
    return {
        "id": f"mix-{_slug(entity)}",
        "kind": "mix",
        "entity": entity,
        "insight_headline": ins["headline"],
        "title": f"What's behind {_poss(entity)} surge?",
        "big": f"{total} new connections",
        "sub": (f"in the last {ins['window_days']} days · none in the prior "
                f"{ins['baseline_days']} days" if not prior_total else
                f"in the last {ins['window_days']} days · was "
                f"{_per_month(prior_total, ins['baseline_days'])} a month before"),
        "verdict": _MIX_VERDICT[lead].format(e=entity),
        "hashtags": _tags(hashtags.camel_tag(entity), _GROUP_TAG.get(lead), _sector_tag(sector)),
        "sentence": lead_txt,
        "data": {"now": now, "before": before, "now_total": total, "before_total": prior_total,
                 "before_days": ins["baseline_days"], "now_days": ins["window_days"]},
    }


def _grounding_card(ins: dict, now_rows, sector: str = "") -> dict[str, Any]:
    entity = ins["entity"]
    ev: dict[str, int] = {}
    for _rel, et, n in now_rows:
        ev[et] = ev.get(et, 0) + n
    total = sum(ev.values())
    reported = ev.get("direct", 0) + ev.get("web_grounded", 0)
    pct = round(100 * reported / total) if total else 0
    inferred = total - reported
    return {
        "id": f"grounding-{_slug(entity)}",
        "kind": "grounding",
        "entity": entity,
        "insight_headline": ins["headline"],
        "title": f"How much of {_poss(entity)} story is confirmed?",
        "big": f"{pct}% confirmed",
        "sub": f"of {total} connections in the last {ins['window_days']} days",
        "hashtags": _tags(hashtags.camel_tag(entity), _sector_tag(sector)),
        "verdict": ("Solid: mostly backed by reports and sources." if pct >= 80 else
                    "Mostly reported, some of it inferred." if pct >= 60 else
                    "Mixed: about half is our own inference." if pct >= 40 else
                    "Early signal: mostly inference, not yet reported."),
        "sentence": (f"{reported} are reported or cited by sources; "
                     f"{inferred} {'is' if inferred == 1 else 'are'} our model's inference."),
        "data": {k: ev.get(k, 0) for k in ("direct", "web_grounded", "inferred", "speculative")},
    }


def _concentration(con, sector: str, rel: str, where: str) -> tuple[dict[str, int], int]:
    """(edges per name, total edges). For two-sided types (partners/integrates/
    competes) a name's count is the edges it is on, so shares are "in X% of
    them" and never sum past 100% of a doubled total."""
    side = _COUNT_SIDE.get(rel)
    ends = {"a": "r.entity_a_id", "b": "r.entity_b_id"}
    cols = [ends[side]] if side else [ends["a"], ends["b"]]
    counts: dict[str, int] = {}
    for col in cols:
        for name, n in con.execute(
            f"""
            SELECT e.name, COUNT(*)
            FROM relationships r
            JOIN catalysts c ON c.catalyst_id = r.catalyst_id
            JOIN entities e ON e.entity_id = {col}
            WHERE c.sector = ? AND r.rel_type = ? AND r.status IN {_LIVE_STATUSES} AND {where}
            GROUP BY 1
            """,
            [sector, rel],
        ).fetchall():
            counts[name] = counts.get(name, 0) + n
    edges = con.execute(
        f"""
        SELECT COUNT(*) FROM relationships r
        JOIN catalysts c ON c.catalyst_id = r.catalyst_id
        WHERE c.sector = ? AND r.rel_type = ? AND r.status IN {_LIVE_STATUSES} AND {where}
        """,
        [sector, rel],
    ).fetchone()[0]
    return counts, edges


def _hhi(counts: dict[str, int]) -> float:
    tot = sum(counts.values())
    return sum((n / tot) ** 2 for n in counts.values()) if tot else 0.0


def _concentration_card(con, sector: str, ins: dict, recent: str, base: str) -> dict[str, Any] | None:
    rel = ins["rel_type"]
    (now, total), (prior, prior_total) = (_concentration(con, sector, rel, recent),
                                          _concentration(con, sector, rel, base))
    named = sorted(((n, c) for n, c in now.items() if not hashtags.is_generic(n)),
                   key=lambda x: (-x[1], x[0]))
    if not named or not total:
        return None
    top = [{"entity": n, "count": c, "share": round(c / total, 3),
            "prior_share": round(prior.get(n, 0) / prior_total, 3) if prior_total else None}
           for n, c in named[:TOP_K]]
    hhi, prior_hhi = _hhi(now), _hhi(prior)
    lead = top[0]
    pct = round(100 * lead["share"])
    level = "Spread out" if pct < 25 else "Concentrated"
    tied = [t["entity"] for t in top if t["count"] == lead["count"]]
    if len(tied) > 1:
        names = ", ".join(tied[:-1]) + f" and {tied[-1]}"
        was = ""      # "was X%" belongs to one name; the chart shows each
        trend = f"{level}: {names} are tied at {pct}% each."
    elif lead["prior_share"]:
        was_pct = round(100 * lead["prior_share"])
        change = ("up from" if pct > was_pct else "down from" if pct < was_pct else "same as")
        was = f" · was {was_pct}%"
        trend = f"{level}: {lead['entity']} is in {pct}% of them, {change} {was_pct}% before."
    else:
        was = " · new at the top"
        trend = f"{level}: {lead['entity']} leads with {pct}%, and wasn't on the list before."
    return {
        "id": f"concentration-{rel}",
        "kind": "concentration",
        "rel_type": rel,
        "insight_headline": ins["headline"],
        "title": _CONC_TITLE.get(rel, f"Where are the sector's {_phrase(rel, 2)} going?"),
        "big": (f"{len(tied)}-way tie at {pct}%" if len(tied) > 1 else f"{lead['entity']} {pct}%"),
        "sub": f"of {total} {_phrase(rel, total)} in the last {ins['window_days']} days{was}",
        "hashtags": _tags(*[hashtags.camel_tag(n) for n in tied[:2]],
                          hashtags.TOPIC_TAG.get(rel), _sector_tag(sector)),
        "verdict": (f"{lead['entity']} is becoming the default: a dependency risk."
                    if pct >= 25 and len(tied) == 1 else
                    f"Shared control: {len(tied)} names lead this together."
                    if pct >= 25 else
                    "No single player controls this: the field is open."),
        "sentence": trend,
        "data": {"top": top, "total": total, "prior_total": prior_total,
                 "hhi": round(hhi, 3), "prior_hhi": round(prior_hhi, 3),
                 "names": len(now)},
    }


def build_recap(con: duckdb.DuckDBPyConnection, sector: str,
                insights: list[dict[str, Any]], today: date | None = None) -> dict[str, Any] | None:
    """Recap block for this ISO week, or None when there's nothing to chart.
    Never raises (same contract as detect.graph_insights)."""
    try:
        entities = [i for i in insights if i.get("type") == "chokepoint"][:2]
        trend = next((i for i in insights if i.get("type") == "velocity"), None)
        if not entities and not trend:
            return None
        sample = (entities or [trend])[0]
        recent, base = _windows(sample["window_days"], sample["baseline_days"])
        mix_cards, ground_cards = [], []
        for ins in entities:
            now_rows = _entity_counts(con, sector, ins["entity"], recent)
            prior_rows = _entity_counts(con, sector, ins["entity"], base)
            mix_cards.append(_mix_card(ins, now_rows, prior_rows, sector))
            ground_cards.append(_grounding_card(ins, now_rows, sector))
        conc = _concentration_card(con, sector, trend, recent, base) if trend else None
        # Order = rotation/free-tier priority: the lead entity's mix first,
        # then the sector view, then the rest.
        cards = mix_cards[:1] + ([conc] if conc else []) + ground_cards[:1] + mix_cards[1:] + ground_cards[1:]
        today = today or date.today()
        iso = today.isocalendar()
        return {
            "week": f"{iso[0]}-W{iso[1]:02d}",
            "as_of": today.isoformat(),
            "window_days": sample["window_days"],
            "baseline_days": sample["baseline_days"],
            "groups": [g for g, _ in GROUPS] + ["Other"],
            # Digest default: the named entities, the trend topic, the sector.
            "hashtags": _tags(*[hashtags.camel_tag(i["entity"]) for i in entities],
                              hashtags.TOPIC_TAG.get(trend["rel_type"]) if trend else None,
                              _sector_tag(sector)),
            "cards": cards,
        }
    except Exception:
        import logging
        logging.getLogger("robotics.recap").exception("build_recap failed (non-fatal)")
        return None
