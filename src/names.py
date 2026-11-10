"""Deterministic name normalization for entity resolution (no LLM, no fuzz).

strict_key — "is this the same written name?": case, punctuation, spacing and
             legal suffixes ignored, word order kept. "Samsung SDS, Inc." ==
             "samsung sds"; "Bank of China" != "China Bank".
loose_key  — "is this the same generic phrase?": also singular/plural, filler
             words, demonym → place, word order. "leading logistics companies in
             China" == "Chinese leading logistic company". NEVER used to merge
             two named organizations — only for generic phrases (is_generic) and
             as an extra LLM candidate signal.
is_generic — a descriptor, not a named organization: after loose_key every word
             is a generic noun/adjective or a place ("Chinese telecom company",
             "Customers", "humanoid robot makers").

Spec: docs/ticket-ingest-entity-resolution.md §3.1–3.2.
"""
from __future__ import annotations

import re
import unicodedata

_LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company_suffix", "ltd", "limited", "plc", "llc",
          "gmbh", "ag", "sa", "se", "nv", "bv", "kk", "pte", "pty", "holdings", "holding"}
_FILLER = {"the", "of", "in", "a", "an", "and", "for", "its", "s", "from", "based", "s'", "to", "on", "at", "by", "with"}

# demonym / short form → place token (one word per place, so keys collide on purpose)
_PLACE = {
    "chinese": "china", "china": "china", "prc": "china", "beijing": "china",
    "japanese": "japan", "japan": "japan",
    "korean": "korea", "korea": "korea", "south": "", "southkorea": "korea",
    "american": "us", "america": "us", "us": "us", "u.s": "us", "usa": "us", "united": "", "states": "us",
    "european": "europe", "europe": "europe", "eu": "europe",
    "german": "germany", "germany": "germany", "french": "france", "france": "france",
    "british": "uk", "uk": "uk", "britain": "uk", "english": "uk",
    "indian": "india", "india": "india", "taiwanese": "taiwan", "taiwan": "taiwan",
    "israeli": "israel", "israel": "israel", "canadian": "canada", "canada": "canada",
    "swiss": "switzerland", "switzerland": "switzerland", "italian": "italy", "italy": "italy",
    "singaporean": "singapore", "singapore": "singapore", "australian": "australia", "australia": "australia",
    "western": "west", "global": "global", "asian": "asia", "asia": "asia", "middle": "", "east": "",
    "gulf": "gulf", "domestic": "domestic", "international": "international", "worldwide": "global",
}
PLACES = {v for v in _PLACE.values() if v}

# Words that describe a KIND of organization or a market, never a name by themselves.
_GENERIC = {
    "company", "firm", "startup", "start", "up", "maker", "manufacturer", "producer", "provider", "supplier", "vendor",
    "customer", "client", "investor", "operator", "developer", "player", "peer", "rival", "competitor", "incumbent",
    "buyer", "seller", "retailer", "user", "adopter", "deployer", "integrator", "distributor", "shipper",
    "sector", "industry", "market", "segment", "ecosystem", "space", "field",
    "leading", "major", "top", "large", "big", "small", "emerging", "established", "new", "next", "generation",
    "local", "regional", "national", "private", "public", "state", "owned", "listed", "traditional", "legacy",
    "high", "low", "cost", "end", "industrial", "commercial", "consumer", "enterprise", "service", "services",
    "logistics", "logistic", "shipping", "delivery", "warehouse", "warehousing", "telecom", "telecommunication",
    "automotive", "auto", "car", "carmaker", "automaker", "ev", "electric", "vehicle", "battery", "chip", "chipmaker",
    "semiconductor", "robotics", "robotic", "robot", "humanoid", "ai", "embodied", "physical", "automation",
    "manufacturing", "factory", "construction", "defense", "defence", "healthcare", "medical", "hospital",
    "research", "lab", "laboratory", "university", "institution", "institute", "school",
    "tech", "technology", "software", "hardware", "platform", "solution", "system", "model", "foundation",
    "venture", "capital", "vc", "fund", "bank", "insurer", "government", "authority", "agency", "regulator",
    "group", "community", "consortium", "alliance", "organization", "organisation", "body", "firms",
}

_WORD = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKC", s or "").lower().replace("’", "'").replace("&", " and ")
    s = s.replace("u.s.", "us ").replace("u.k.", "uk ")
    return _WORD.findall(s)


def mentioned_in(text: str, name: str) -> bool:
    """Case-insensitive, whole words: does `name` (or its form without a legal
    suffix, "Tesla Inc." → "Tesla") appear in `text`?"""
    t = " " + case_key(text) + " "
    return any(k and f" {k} " in t for k in {case_key(name), strict_key(name)})


def case_key(s: str) -> str:
    """Same text ignoring only case, punctuation and spacing (legal suffix kept)."""
    return " ".join(_words(s))


def strict_key(s: str) -> str:
    """Same written name: lowercase, punctuation/spacing/legal suffix ignored, order kept."""
    w = _words(s)
    while len(w) > 1 and w[-1] in _LEGAL:
        w.pop()
    return " ".join(w)


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def loose_key(s: str) -> str:
    """Same generic phrase: + singular, filler words dropped, demonym → place, order-free."""
    out = []
    for w in _words(s):
        w = _PLACE.get(w, _singular(w)) if w in _PLACE else _singular(w)
        if w and w not in _FILLER and w not in _LEGAL:
            out.append(w)
    return " ".join(sorted(set(out)))


_COLLECTIVE = {"sector", "industry", "market", "segment", "ecosystem", "community"}   # not "space"/"field": Space AI, Field AI
_GOVERNMENT = {"government", "state", "authority", "authorities", "administration", "ministry", "regulator"}


def is_generic(s: str) -> bool:
    """A descriptor, not a named organization. Word lists alone can't tell
    "China Telecom" (a company) from "Chinese telecom company", so the ORIGINAL
    casing decides: every word must be generic or a place, AND at least one
    generic word is written lowercase ("humanoid robots", "Gulf investors") or
    is plural / collective ("Investors", "Robotics Manufacturers", "Physical
    AI Sector"). Title-Case singular names stay names ("Humanoid", "Bank of
    China", "Venture Capital"). Government phrases are named bodies → normal
    lookup ("Chinese government" is an alias of China)."""
    raw = re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKC", s or "").replace("’", "'"))
    words = [(w, w.lower()) for w in raw if w.lower() not in _FILLER and w.lower() not in _LEGAL]
    if not words:
        return False
    norm = [(w, _PLACE.get(lw, _singular(lw)) if lw in _PLACE else _singular(lw), lw) for w, lw in words]
    norm = [(w, n, lw) for w, n, lw in norm if n]       # "south", "united" fold into their place word
    if not norm or not all(n in _GENERIC or n in PLACES for _w, n, _lw in norm):
        return False
    if any(lw in _GOVERNMENT or n in _GOVERNMENT for _w, n, lw in norm):   # "Local Governments" too
        return False
    gen = [(w, n, lw) for w, n, lw in norm if n in _GENERIC]
    if not gen:
        return False
    lowercase = any(w == lw and not w.isdigit() for w, _n, lw in gen)
    # n != lw: it was a plural — unless the word itself is generic as written
    # ("logistics", "robotics", "electronics" end in s but aren't plurals).
    collective = any(lw in _COLLECTIVE or (n != lw and lw not in _GENERIC) for w, n, lw in gen)
    return lowercase or collective


def place_of(s: str) -> str | None:
    """The single place a generic phrase is about ("Chinese telecom company" → "china"), if any."""
    places = [t for t in loose_key(s).split() if t in PLACES and t not in {"global", "domestic", "international", "west"}]
    return places[0] if len(set(places)) == 1 else None
