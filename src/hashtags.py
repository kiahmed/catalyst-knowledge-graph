"""Per-catalyst hashtags — deterministic, no LLM.

Consumer: soljet-postiz, which today builds its own tags from just the entity
names + a sector list. The KG knows more — which relationship the card is
actually about, whether an entity name is a real brand or a generic phrase
("European Component Suppliers"), and the headline's subject — so tags are
decided here and stored on the catalyst.

Order is priority order; consumers take a prefix to fit their char budget:
  1. up to 2 entity tags   — the brands the post is about (most specific)
  2. 1 topic tag           — from the card's top relationship type
  3. up to 2 theme tags    — keyword themes from headline + finding
  4. 1 sector tag          — always present, the reliable floor

Never an LLM: it invents tags that don't exist or don't trend.
"""
from __future__ import annotations

import re
from typing import Any

MAX_TAGS = 6
MAX_ENTITY_TAGS = 2
MAX_THEME_TAGS = 2
MAX_TAG_LEN = 22           # "#" + 21 chars; longer brand tags don't get used

SECTOR_TAG = {
    "robotics": "#Robotics",
    "ai stack": "#AI",
    "crypto": "#Crypto",
    "space & defense": "#SpaceTech",
    "power & energy": "#Energy",
    "strategic minerals": "#CriticalMinerals",
}

TOPIC_TAG = {
    "partners_with": "#Partnership",
    "integrates_with": "#Integration",
    "pilots": "#Pilot",
    "deploys": "#Deployment",
    "acquires": "#Acquisition",
    "invests_in": "#Funding",
    "supplies": "#SupplyChain",
    "competes_with": "#Competition",
    "displaces": "#Disruption",
    "benchmarks_against": "#Benchmarks",
    "regulates": "#Regulation",
    "litigates_against": "#Litigation",
    "hires_from": "#Talent",
    "spins_out_from": "#Spinout",
    "built_on": "#TechStack",
}

# Headline event → topic. Checked BEFORE the edge-derived topic: the event a
# headline announces is the story, while edges can be all second-order
# inference (NEURA "Secures $1.4 Billion" has only inferred competitor edges,
# which would otherwise tag a funding story #Competition).
EVENT_RULES: list[tuple[str, str]] = [
    # Order matters — first match wins, most specific events first.
    (r"\b(raises?|secures? \$|funding|series [a-f]\b|valuation|\bipo\b|invest(s|ment)\b)", "#Funding"),
    (r"\b(acquires?|acquisition|buys|merger|takeover)\b", "#Acquisition"),
    (r"\b(sues|lawsuit|litigation)\b", "#Litigation"),
    # Before ProductLaunch: "South Korea Launches National ... Strategy" is
    # policy, not a product.
    (r"\b(national \w+( \w+)? strategy|government|ministry|policy|subsid\w*)\b", "#TechPolicy"),
    (r"\b(regulat\w*|legislation|export control|ban(s|ned)?)\b", "#Regulation"),
    (r"\b(challeng\w*|rivals?|takes on|versus|vs\.?)\b", "#Competition"),
    (r"\b(partners?|partnership|teams up|alliance|collaborat\w*)\b", "#Partnership"),
    (r"\b(launch(es)?|unveils?|introduces?|debuts?|releases?)\b", "#ProductLaunch"),
]

# Edge evidence weight for choosing which entities name the story: stated
# facts before inference.
_EVIDENCE_RANK = {"direct": 3, "web_grounded": 2, "inferred": 1, "speculative": 0}

# (pattern, tag) — first matches win, capped at MAX_THEME_TAGS. Matched on
# headline + finding, lower-cased. Kept narrow: a theme tag must be something
# people actually search/follow.
THEME_RULES: list[tuple[str, str]] = [
    (r"\bhumanoids?\b", "#Humanoids"),
    (r"\b(physical ai|embodied ai|vla\b|vision-language-action|robot foundation model)", "#PhysicalAI"),
    (r"\b(robotaxi|self-driving|autonomous (vehicle|driving|truck))", "#AutonomousVehicles"),
    (r"\b(warehouse|fulfil+ment|intralogistics)", "#WarehouseAutomation"),
    (r"\b(surgical|surgery|medical robot|hospital)", "#MedTech"),
    (r"\b(drones?|uavs?|unmanned aerial)", "#Drones"),
    (r"\b(cobots?|collaborative robot)", "#Cobots"),
    (r"\b(defen[cs]e|military|pentagon|dod)\b", "#DefenseTech"),
    (r"\b(semiconductor|chips?|gpus?|foundry)\b", "#Semiconductors"),
    # "farm" alone matched "data learning farms" — require ag context.
    (r"\b(agricultur\w*|farming|crops?\b|harvesting)", "#AgTech"),
    (r"\b(factory|manufactur)", "#Manufacturing"),
    (r"\b(open[- ]source)", "#OpenSource"),
]

_COMPANY_TYPES = {"public_company", "private_company", "company"}
# A plural group noun as the last word marks a category, not a brand:
# "European Component Suppliers", "EdTech Robotics Companies".
_GENERIC_TAIL = re.compile(
    r"\b(companies|suppliers|manufacturers|makers|firms|startups|players|"
    r"vendors|providers|operators|developers|investors|customers|industry|"
    r"sector|market|markets|ecosystem)\s*$", re.I)
_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc|ltd|llc|plc|corp|corporation|co|gmbh|ag|sa|se|nv|holdings?)\.?\s*$", re.I)


def camel_tag(name: str) -> str | None:
    """'Figure AI' -> '#FigureAI'. None for names that make bad tags."""
    if not name:
        return None
    base = re.sub(r"\(.*?\)", " ", name)          # drop "(NVDA)", "(066570.KS)"
    base = _LEGAL_SUFFIX.sub("", base.strip())
    if _GENERIC_TAIL.search(base):
        return None
    words = re.findall(r"[A-Za-z0-9]+", base)
    if not words:
        return None
    tag = "#" + "".join(w[:1].upper() + w[1:] for w in words)
    if len(tag) > MAX_TAG_LEN or tag[1:].isdigit():
        return None
    return tag


def tags_for(
    sector: str,
    headline: str,
    finding: str,
    relationships: list[dict[str, Any]],
    entity_types: dict[str, str],
) -> list[str]:
    """relationships: [{"from","to","rel","confidence"}] for this catalyst.
    entity_types: name -> entity type."""
    out: list[str] = []

    def add(t: str | None) -> None:
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)

    # 1. entities — walk edges strongest-first so the tags name the story's
    #    actual subjects, not whichever entity sorts first alphabetically.
    ents = 0
    ranked = sorted(
        relationships,
        key=lambda r: (_EVIDENCE_RANK.get(r.get("evidence_type") or "direct", 0),
                       r.get("confidence") or 0),
        reverse=True,
    )
    for r in ranked:
        for name in (r.get("from"), r.get("to")):
            if ents >= MAX_ENTITY_TAGS:
                break
            if entity_types.get(name) not in _COMPANY_TYPES:
                continue
            t = camel_tag(name)
            if t and t.lower() not in {x.lower() for x in out}:
                out.append(t)
                ents += 1

    # 2. topic — the headline's announced event, else the best-evidenced edge
    head_l = (headline or "").lower()
    topic = next((tag for pat, tag in EVENT_RULES if re.search(pat, head_l)), None)
    if topic is None and ranked:
        topic = TOPIC_TAG.get(ranked[0].get("rel", ""))
    add(topic)

    # 3. themes
    text = f"{headline or ''} {finding or ''}".lower()
    themes = 0
    for pat, tag in THEME_RULES:
        if themes >= MAX_THEME_TAGS:
            break
        if re.search(pat, text) and tag.lower() not in {x.lower() for x in out}:
            out.append(tag)
            themes += 1

    # 4. sector floor — always present
    add(SECTOR_TAG.get((sector or "").strip().lower())
        or (camel_tag(sector) if sector else None))

    # keep the sector tag even when we are over the cap
    if len(out) > MAX_TAGS:
        sector_tag = out[-1]
        out = out[:MAX_TAGS - 1] + [sector_tag]
    return out


# ── Storage (catalysts.hashtags, JSON array) ───────────────────────

def compute_for_catalyst(con, catalyst_id: int) -> list[str]:
    """Build tags from what is already in the DB for this catalyst."""
    row = con.execute(
        "SELECT sector, headline, raw_finding FROM catalysts WHERE catalyst_id = ?",
        [catalyst_id],
    ).fetchone()
    if not row:
        return []
    sector, headline, finding = row
    rels = [
        {"from": a, "to": b, "rel": rt, "confidence": c, "evidence_type": et}
        for (a, b, rt, c, et) in con.execute(
            """
            SELECT ea.name, eb.name, r.rel_type, r.confidence, r.evidence_type
            FROM relationships r
            JOIN entities ea ON ea.entity_id = r.entity_a_id
            JOIN entities eb ON eb.entity_id = r.entity_b_id
            WHERE r.catalyst_id = ? AND r.status IN ('active', 'materialized')
            """,
            [catalyst_id],
        ).fetchall()
    ]
    types = dict(con.execute(
        """
        SELECT DISTINCT e.name, e.type
        FROM relationships r
        JOIN entities e ON e.entity_id IN (r.entity_a_id, r.entity_b_id)
        WHERE r.catalyst_id = ?
        """,
        [catalyst_id],
    ).fetchall())
    return tags_for(sector, headline, finding, rels, types)


def store_for_catalyst(con, catalyst_id: int) -> list[str]:
    import json

    tags = compute_for_catalyst(con, catalyst_id)
    con.execute(
        "UPDATE catalysts SET hashtags = ? WHERE catalyst_id = ?",
        [json.dumps(tags), catalyst_id],
    )
    return tags


def backfill(con, force: bool = False) -> int:
    """Tag every catalyst that has none yet (force=True re-tags everything,
    for when the rules change). Deterministic and cheap — no network."""
    where = "" if force else "WHERE hashtags IS NULL"
    ids = [r[0] for r in con.execute(f"SELECT catalyst_id FROM catalysts {where}").fetchall()]
    for cid in ids:
        store_for_catalyst(con, cid)
    return len(ids)
