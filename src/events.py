"""Card-level event type — deterministic, no LLM (2026-11-11, docs/ticket-card-event-type.md).

What HAPPENED in a card's story, as one label: an IPO, an acquisition, a funding round, a product launch,
a factory / production start, a deployment / contract / order, a partnership, a regulation, a research
result, a leadership change. It is a property of the card, not an edge, so
it can say "Tesla starts Optimus production" without a self-loop. NULL = none of these (market analysis,
commentary, forecasts) — never forced.

Only the headline decides (the story's sentences mention every kind of event as context — tested: using
them as a fallback mostly added wrong labels). The EARLIEST cue wins, since the headline's main verb comes
first ("X raises $100M to build a factory" is funding); an IPO cue wins from anywhere; "X launches a
factory / a fund / a national strategy" goes by the object. No cue → NULL.
"""
from __future__ import annotations

import re

_I = re.I
EVENT_TYPES = ("ipo_listing", "m_and_a", "funding", "product_launch", "production_facility", "deployment",
               "partnership", "regulation", "research", "leadership")

_MONEY = r"(?:\$|€|£|¥|US\$|RMB ?|HK\$)?[\d.,]+ ?(?:[MBK]\b|million|billion|bn|mn)"
_CUES: dict[str, re.Pattern] = {
    "ipo_listing": re.compile(r"\b(IPO|IPOs|initial public offering|go(?:es|ing)? public|public listing|listed on|lists on|"
                              r"(?:Nasdaq|NYSE|HKEX|KOSDAQ|STAR Market|stock exchange) (?:debut|listing)|SPAC|prospectus|"
                              r"(?:debut\w*|launch\w*|list\w*|trad\w*) on (?:the )?(?:Nasdaq|NYSE|HKEX|KOSDAQ|STAR Market))\b", _I),
    "m_and_a": re.compile(r"\b(acquir(?:e|es|ed|ing)|acquisition|merg(?:e|es|ed|er|ing)|buy(?:s|ing)?|bought|buyout|"
                          r"takeover|takes? over|full ownership|majority stake|spin-?offs?|spins? off)\b", _I),
    "funding": re.compile(r"\b(rais(?:e|es|ed|ing)|funding|Series [A-H]\+?|seed round|pre-seed|financing|"
                          r"round of|valuation|valued at|backs|invests?|investment|grants?|subsid(?:y|ies|i[sz]es)|"
                          r"secur(?:e|es|ed|ing) (?:" + _MONEY + r"|billions|millions|(?:major |mega |new )?(?:funding|rounds?|capital)|mega rounds?)\b)", _I),
    "product_launch": re.compile(r"\b(launch(?:es|ed|ing)?|unveil(?:s|ed|ing)?|debut(?:s|ed)?|introduc(?:e|es|ed|ing)|"
                                 r"releas(?:e|es|ed)|rolls? out|rolled out|reveal(?:s|ed)?|showcas(?:e|es|ed)|"
                                 r"open[- ]sourc(?:e|es|ed)|(?:exits?|emerges?) (?:from )?stealth)\b", _I),
    "production_facility": re.compile(r"\b(factory|factories|plant|gigafactory|production line|manufacturing (?:hub|base|site)|"
                                      r"mass[- ]produc\w+|(?:begin|start|ramp|initiat|scal|expand|boost)\w* (?:\S+ ){0,4}production|"
                                      r"production (?:ramps?|capacity|starts?|begins?)|commission(?:s|ed))\b", _I),
    "deployment": re.compile(r"\b(deploy(?:s|ed|ing|ment|ments)?|deliver(?:s|ed)? (?:\S+ ){0,4}to|(?<!contract )contracts?(?! manufactur)|orders?|"
                             r"pre-?orders?|pilot(?:s|ed|ing)?|commerciali[sz](?:e|es|ed|ing|ation)|"
                             r"selects?|selected)\b", _I),
    "partnership": re.compile(r"\b(partner(?:s|ed|ing|ship|ships)?|collaborat\w+|teams? up|alliance|"
                              r"joint ventures?|(?-i:JVs?)|MoU|memorandum|tie-up|join(?:s|ed)? forces)\b", _I),
    "regulation": re.compile(r"\b(bans?|banned|blocks?|blocked|regulat\w+|law|laws|bill|legislation|FCC|tariffs?|export controls?|"
                             r"sanctions?|polic(?:y|ies)|AI Act|designat(?:es|ed) (?:\S+ ){0,3}(?:top |national |strategic )?priority|standards|standardi[sz]\w+|probes?|scrutiny)\b", _I),
    "research": re.compile(r"\b(research(?:ers)?|study|paper|arXiv|breakthrough|university|scientists?)\b", _I),
    "leadership": re.compile(r"\b(appoint(?:s|ed|ment)?|names? (?:new )?(?:CEO|CTO|CFO|COO|chief|president|chair)|"
                             r"steps? down|resign(?:s|ed|ation)?|hires? (?:former )?(?:\w+ ){0,3}(?:CEO|CTO|chief|head|lead|executive|veteran))\b", _I),
}
# "X launches a factory / a national initiative": the object decides.
_LAUNCH_OBJECT = {
    "production_facility": re.compile(r"^(?:\S+ ){0,3}?(?:factory|plant|production line|gigafactory)\b", _I),
    "funding": re.compile(r"^(?:\S+ ){0,6}?fund\b", _I),
    "regulation": re.compile(r"^(?:\S+ ){0,7}?(?:national|sovereign|government|initiative|programme|policy|"
                             r"fast[- ]track)\b", _I),
}


def _earliest(text: str) -> str | None:
    if _CUES["ipo_listing"].search(text):                # specific enough to win from anywhere in the text
        return "ipo_listing"
    best: tuple[int, int, str, int] | None = None
    for rank, (et, rx) in enumerate(_CUES.items()):     # ties at one position: the more specific type first
        m = rx.search(text)
        if m and (best is None or (m.start(), rank) < best[:2]):
            best = (m.start(), rank, et, m.end())
    if best and best[2] == "product_launch":
        for et, rx in _LAUNCH_OBJECT.items():
            if rx.search(text[best[3]:].lstrip()):
                return et
    return best[2] if best else None


def classify(headline: str | None) -> str | None:
    """The card's event type from its headline, or None. Pure function."""
    return _earliest(headline or "")


def refresh(con, catalyst_ids=None) -> int:
    """Recompute catalysts.event_type from each card's headline; write only changed rows. None → every card
    (the backfill; self-heals when the rules change). Deterministic, local, milliseconds. Returns rows changed."""
    where, params = "", []
    if catalyst_ids is not None:
        params = sorted({int(c) for c in catalyst_ids})
        if not params:
            return 0
        where = f"WHERE catalyst_id IN ({','.join('?' * len(params))})"
    rows = con.execute(f"SELECT catalyst_id, headline, event_type FROM catalysts {where}", params).fetchall()
    changes = [(new, cid) for cid, head, cur in rows if (new := classify(head)) != cur]
    if changes:
        con.execute("""UPDATE catalysts SET event_type = n.t
                       FROM (SELECT unnest(?::VARCHAR[]) AS t, unnest(?::INTEGER[]) AS id) n
                       WHERE catalysts.catalyst_id = n.id""", [[c[0] for c in changes], [c[1] for c in changes]])
    return len(changes)
