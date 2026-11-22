"""Funding amount per card — deterministic, no LLM (2026-11-21).

For a card whose event_type is "funding" (src/events.py), read from the
HEADLINE: who raised, how much, which round. Stored on the card, not on each
investor edge — one $1.4B round with five investors is $1.4B, not $7B.

The UI sums distinct rounds per company for the selected time window (the same
round covered by several cards is one round). Nothing is stored per entity, so
merges, deletions and window changes need no recompute.

kind:
  round      money raised by `target` (amount may be unknown: "secures Series A funding")
  valuation  only a valuation is reported ("achieves $6B valuation") — not money raised
  fund       an investor launches/closes a fund — capital to deploy, not a company raise
Headlines about a deal falling apart, or money for contracts/deals, give None.
Currency → USD uses a fixed approximate table (FX_USD); amount + currency are kept as reported.
"""
from __future__ import annotations

import re
from typing import Any

from .story import COMMON_WORDS, _TICKER

_I = re.I
FX_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CNY": 0.14, "JPY": 0.0067, "HKD": 0.128, "KRW": 0.00073}
_CUR_SYM = {"US$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY", "₩": "KRW", "RMB": "CNY",
            "CNY": "CNY", "HK$": "HKD", "dollars": "USD", "euros": "EUR", "yuan": "CNY", "USD": "USD", "EUR": "EUR",
            "JPY": "JPY", "yen": "JPY"}
# "¥" is both yuan and yen: yen when the headline says so, else yuan (most ¥ headlines here are Chinese).
_JAPAN = re.compile(r"\b(Japan|Japanese|yen|JPY|Tokyo)\b", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
          "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}

_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_AMOUNT = re.compile(
    r"(?P<cur>US\$|HK\$|\$|€|£|¥|₩|RMB ?|CNY ?|JPY ?)?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?|(?:" + _UNITS + r")\s+hundred|" + "|".join(_WORDS) + r")\s?"
    r"(?P<scale>billion|million|thousand|bn|mn|[BMK])\b"
    r"(?:\s+(?P<cur2>dollars|euros|yuan|yen|USD|EUR|JPY)\b)?", _I)
_ROUND = re.compile(r"\b(Series [A-H]\d?\+?|Pre-?Series [A-H]|Pre-?seed|Seed|Angel\+?|Pre-?A\+?|Debt|Growth|Strategic)\b"
                    r"(?:\s+(?:round|funding|financing|investment))?", _I)
_VALUATION_AFTER = re.compile(r"^\s*(?:\S+\s+){0,1}valuation\b", _I)
_VALUATION_BEFORE = re.compile(r"(?:valued at|valuation of|valuation to|reach(?:es|ing)?|at an?|tops|soars past|hits)\s+$", _I)
_RAISE_VERB = re.compile(r"\b(rais\w*|secur\w*|clos\w*|lands?|bags?|nets?|gets?|receiv\w*|invests?|investment|backs|"
                         r"funding|financing|round)\b", _I)
_FUND = re.compile(r"\b(?:launch\w*|clos\w*|rais\w*|unveil\w*|form\w*|creat\w*|debut\w*)\b.{0,60}?\bfund\b"
                   r"|\b(?:venture|investment|growth|robotics|AI|innovation) fund\b", _I)
_NEGATIVE = re.compile(r"\b(collaps\w*|cancel\w*|scrap\w*|falls? through|fell through|withdraw\w*|abandon\w*|"
                       r"halt\w*|blocked|rejects?)\b", _I)
_DEAL_AFTER = re.compile(r"^\s*(?:\S+\s+){0,3}(?:deals?|contracts?|orders?|revenue|sales)\b", _I)
_BARE_OK = re.compile(r"\b(?:secur\w*|rais\w*|lands?|nets?|bags?|over|closes?)\s+$", _I)
# Sector roundups and non-money "raising": not one company's round.
_NOT_A_ROUND = re.compile(r"\b(venture capital funding|investment surges|in H[12]\b|record high|"
                          r"raising (?:\w+ ){0,3}(?:concerns?|questions?|doubts?|alarm|eyebrows))", _I)
# …or a group as the SUBJECT ("Physical AI Startups Secure $47B", "Humanoid Robotics Sector Secures …")
_GROUP_SUBJECT = re.compile(r"\b(startups|sector|industry|VCs)\b", _I)
_SUBJECT_VERB = re.compile(r"\b(secur\w*|rais\w*|lands?|nets?|bags?|clos\w*|receiv\w*|gets?|reach\w*|achiev\w*|"
                           r"targets?|seeks?|eyes|nears?)\b", _I)
_INVEST_CUE = re.compile(r"\b(?:invests?|investment|stake|backs|backing|(?:co-)?leads?)\b", _I)
_INTO = re.compile(r"\b(?:co-)?leads?\b.{0,40}?\b(?:for|in)\s+$|"
                   r"\b(?:invests?|investment|stake|backs|backing|bets?)\b(?:\s+(?:\S+\s+){0,4}?)?\s*\b(?:in|into)\s+$|"
                   r"\b(?:backs|backing)\s+$", _I)


def _num(s: str) -> float:
    s = s.lower()
    if s.endswith("hundred") and " " in s:                     # "five hundred"
        return float(_WORDS[s.split()[0]] * 100)
    return float(_WORDS[s]) if s in _WORDS else float(s.replace(",", ""))


def _amounts(text: str) -> list[dict[str, Any]]:
    out = []
    for m in _AMOUNT.finditer(text):
        scale = m.group("scale").lower()
        if scale in ("b", "m", "k") and not m.group("cur") and not m.group("cur2") \
                and not _BARE_OK.search(text[:m.start()]):
            continue                                            # "3B model", "7M users" — need a currency or a raise verb
        cur = (m.group("cur") or m.group("cur2") or "$").strip()
        cur = "JPY" if cur == "¥" and _JAPAN.search(text) else \
            _CUR_SYM.get(cur, _CUR_SYM.get(cur.lower(), _CUR_SYM.get(cur.upper(), "USD")))
        amount = _num(m.group("num")) * _SCALE[scale]
        after, before = text[m.end():], text[:m.start()]
        out.append({"amount": amount, "currency": cur, "start": m.start(),
                    "valuation": bool(_VALUATION_AFTER.match(after) or _VALUATION_BEFORE.search(before)),
                    "deal": bool(_DEAL_AFTER.match(after))})
    return out


def _positions(names: list[str], text: str, aliases: dict[str, set[str]]) -> list[tuple[int, int, str]]:
    """(start, end, name) of each entity's earliest whole-word mention in text (story.py match rules)."""
    hits = []
    for name in names:
        best = None
        for k in aliases.get(name) or {name, _TICKER.sub("", name).strip()}:
            if len(k) < 2:
                continue
            exact = len(k) < 3 or k.lower() in COMMON_WORDS
            m = re.search(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", text, 0 if exact else _I)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), m.end(), name)
        if best:
            hits.append(best)
    return sorted(hits)


def parse(headline: str | None, entity_names: list[str], aliases: dict[str, set[str]] | None = None) -> dict[str, Any] | None:
    """Funding facts from a funding card's headline, or None. Pure function."""
    text = headline or ""
    if not text or _NEGATIVE.search(text) or _NOT_A_ROUND.search(text):
        return None
    verb = _SUBJECT_VERB.search(text)
    if verb and _GROUP_SUBJECT.search(text[:verb.start()]):
        return None
    hits = _positions(entity_names, text, aliases or {})
    amounts = _amounts(text)
    raised = [a for a in amounts if not a["valuation"] and not a["deal"]]
    rnd = _ROUND.search(text)
    round_label = None
    if rnd:
        round_label = re.sub(r"\s+(round|funding|financing|investment)$", "", rnd.group(1), flags=_I)
        round_label = round_label[0].upper() + round_label[1:] if not round_label.lower().startswith("series") \
            else "Series " + round_label.split()[-1].upper()

    if _FUND.search(text):
        kind = "fund"
    elif raised and _RAISE_VERB.search(text):
        kind = "round"
    elif amounts and all(a["valuation"] for a in amounts):
        kind = "valuation"
    elif amounts and all(a["deal"] or a["valuation"] for a in amounts):
        return None
    elif _RAISE_VERB.search(text) or rnd:
        kind = "round"                                          # raise without a stated amount
    else:
        return None

    # Target: "X invests [$N] in T" / "backs T" → the entity after the cue; else the headline's subject.
    target = None
    if kind == "fund":                                          # the manager: named before the word "fund"
        fpos = re.search(r"\bfund\b", text, _I)
        before = [h for h in hits if fpos and h[0] < fpos.start()]
        target = before[0][2] if before else None
    else:
        cue = _INVEST_CUE.search(text)
        if cue and verb and verb.start() < cue.start():
            cue = None                                          # "X secures $15M investment, …": X is the subject
        for s, e, name in hits:
            if _INTO.search(text[:s]):
                target = name
                break
        if target is None and cue is None and hits:
            # the headline's subject; skip a possessive owner ("Rivian CEO's Mind Robotics")
            subj = [h for h in hits if not re.match(r"(?:\s+\w+)?['’]s\b", text[h[1]:])]
            target = (subj or hits)[0][2]
        elif target is None and cue is not None:
            # "X invests in <not a known company>": the money's target is unknown, never the investor
            target = next((n for s, _e, n in hits if s > cue.end()), None)

    pick = raised[0] if kind == "round" and raised else (amounts[0] if kind != "round" and amounts else None)
    amount = pick["amount"] if pick else None
    currency = pick["currency"] if pick else None
    return {
        "kind": kind,
        "target": target,
        "amount": amount,
        "currency": currency,
        "amount_usd": round(amount * FX_USD[currency]) if amount is not None else None,
        "round": round_label if kind == "round" else None,
    }
