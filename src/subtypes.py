"""Relationship subtypes — deterministic, no LLM (2026-11-11).

`rel_type` stays one of the 15 parent types (every count, filter, recap and
consumer keeps working). `rel_subtype` adds finer meaning only where the
mechanism text the extractor already wrote says so explicitly. A subtype always
belongs to its parent, so a "manufactures_for" edge still counts as "supplies".

Rules are ordered; the first match wins; no match → no subtype (plain parent).
Re-running classify() over stored rows is the backfill. Rules were tuned on a
hand-read sample of prod edges (data/subtype_eval_*.txt).
"""
from __future__ import annotations

import re

_I = re.I

# Public money: the source is a government body, or the text names a grant / subsidy / public funding.
# ("subsidiary" is NOT a subsidy; "a government-backed company" describes the target, not this edge.)
_FUNDS = re.compile(r"\b(grants?|granted|subsid(?:y|ies|i[sz]e[sd]?|i[sz]ing)|public funding)\b", _I)
# parent ↔ subsidiary (a invests_in b where b is a's subsidiary / wholly owned)
_OWNS = re.compile(r"\b(subsidiar(?:y|ies)|wholly[- ]owned|owned by|parent company(?! of))\b", _I)
# a company MANUFACTURING for another. Explicit roles always count; a verb counts only when its object is
# the target itself ("manufactures chips for Tesla", "fabrication of Tesla's chip") — a supplier selling its
# own catalogue ("manufactures lidar essential for humanoids like UBTECH's") is plain supplies.
_MFG_ROLE = re.compile(r"\b(contract[- ]manufactur\w*|(?:mass[- ])?(?:manufacturing|production) partner\w*|ODM|EMS provider|foundry partner)\b", _I)
_MFG_VERB = r"(?:manufactur(?:e|es|ing)|fabricat(?:e|es|ing|ion)|assembl(?:e|es|ing))"
_DIST = re.compile(r"\b(distribut(?:e|es|ed|ing|or|ors|ion)|resell\w*|resale|reseller\w*|dealers?|dealership\w*)\b", _I)
_NOT_DIST = re.compile(r"\bdistributed (?:robot\w*|comput\w*|system\w*|environment\w*|ledger\w*|network\w*|"
                       r"architecture\w*|sensing|training|control)\b|\blogistics\b|\bdistribut\w* (?:\S+ ){0,5}across\b", _I)
_LIC = re.compile(r"\b(licens\w*|royalt\w*|IP licen\w*)\b", _I)
_JV = re.compile(r"\b(joint[- ]venture|JV)\b")
_RESEARCH = re.compile(r"\b(research (?:collaboration|partnership|program|center|centre|agreement|lab|laboratory)|"
                       r"joint (?:research|lab|laboratory)|co-research|academic partner\w*|university (?:partner\w*|collaborat\w*)|"
                       r"collaborat\w* (?:\w+ ){0,3}on (?:advanced )?research)\b", _I)
_CODEV = re.compile(r"\b(jointly develop\w*|co-develop\w*|co-engineer\w*|co-design\w*|develop\w* (?:\w+ ){0,3}jointly)\b", _I)
_RIVAL = re.compile(r"\b(directly compet\w*|compet\w* directly|direct (?:competitor|rival|competition)s?|"
                    r"head[- ]to[- ]head|(?:main|primary|key|chief|closest) (?:competitor|rival)s?|"
                    r"named as (?:a |its )?(?:competitor|rival)s?|arch[- ]?rival\w*)\b", _I)

SUBTYPES: dict[str, list[str]] = {
    "supplies": ["manufactures_for", "licenses_to", "sells_through"],
    "invests_in": ["funds", "owns"],
    "partners_with": ["joint_venture", "research_collaboration", "co_development"],
    "competes_with": ["rivals", "pressures"],
}


def _forms(name: str) -> list[str]:
    """How an entity is written in a mechanism: its full name, else its first word (≥ 3 chars)."""
    first = name.split()[0]
    return [name] + ([first] if first != name and len(first) >= 3 else [])


def _name_re(name: str) -> str:
    return "(?:" + "|".join(re.escape(n) for n in _forms(name)) + r")(?![\w-])"


def _first(text: str, name: str | None) -> int:
    """Position of the first whole-word mention of an entity name (or its first word), -1 if absent."""
    if not name:
        return -1
    m = re.search(r"(?<![\w-])" + _name_re(name), text, _I)
    return m.start() if m else -1


def _manufactures_for(text: str, a_name: str | None, b_name: str | None) -> bool:
    if not a_name or not b_name:
        return False                     # direction can't be checked → no subtype (safe default)
    a_at = _first(text, a_name)
    if a_at < 0:
        return False
    b = _name_re(b_name)
    for m in (_MFG_ROLE.search(text),
              re.search(r"\b" + _MFG_VERB + r" (?:[\w'’-]+ ){0,6}?for (?:the |its )?" + b, text, _I),
              re.search(r"\b" + _MFG_VERB + r" (?:of )?(?:the )?" + b + r"['’]s?\b", text, _I)):
        if m and a_at < m.start():       # the source is named before the manufacturing, i.e. it is the maker
            return True
    return False


def classify(rel_type: str, mechanism: str | None, evidence_type: str | None = None, *,
             a_name: str | None = None, b_name: str | None = None, a_type: str | None = None) -> str | None:
    """The subtype for one edge (a --rel_type--> b), or None (plain parent). Pure function."""
    text = mechanism or ""
    if rel_type == "competes_with":
        if not text:
            return None
        return "rivals" if (evidence_type == "direct" or _RIVAL.search(text)) else "pressures"
    if rel_type == "invests_in":
        if a_type == "government" or _FUNDS.search(text):
            return "funds"
        if _OWNS.search(text) or (b_name and re.search(r"\bparent company of (?:the )?" + _name_re(b_name), text, _I)):
            return "owns"
        return None
    if rel_type == "partners_with":
        if _JV.search(text):
            return "joint_venture"
        if _RESEARCH.search(text):
            return "research_collaboration"
        return "co_development" if _CODEV.search(text) else None
    if rel_type == "supplies":
        if _manufactures_for(text, a_name, b_name):
            return "manufactures_for"
        if _LIC.search(text):
            return "licenses_to"
        if _DIST.search(text) and not _NOT_DIST.search(text):
            return "sells_through"
    return None


def parent_of(subtype: str) -> str | None:
    return next((p for p, subs in SUBTYPES.items() if subtype in subs), None)


def refresh(con, catalyst_ids=None) -> int:
    """Recompute relationships.rel_subtype from each row's current type, mechanism, evidence and endpoint
    names/types; write only rows whose value changed. catalyst_ids=None → every row (the backfill, and
    the self-heal when the rules change). Deterministic, local, no network. Returns rows changed."""
    where, params = "", []
    if catalyst_ids is not None:
        params = sorted({int(c) for c in catalyst_ids})
        if not params:
            return 0
        where = f"WHERE r.catalyst_id IN ({','.join('?' * len(params))})"
    rows = con.execute(f"""SELECT r.rel_id, r.rel_type, r.mechanism, r.evidence_type, a.name, b.name, a.type, r.rel_subtype
        FROM relationships r JOIN entities a ON a.entity_id = r.entity_a_id
        JOIN entities b ON b.entity_id = r.entity_b_id {where}""", params).fetchall()
    changes = [(new, rel_id) for rel_id, rt, mech, ev, an, bn, at, cur in rows
               if (new := classify(rt, mech, ev, a_name=an, b_name=bn, a_type=at)) != cur]
    if changes:               # one set-based UPDATE (row-by-row executemany is seconds per thousand in DuckDB)
        con.execute("""UPDATE relationships SET rel_subtype = n.s
                       FROM (SELECT unnest(?::VARCHAR[]) AS s, unnest(?::INTEGER[]) AS id) n
                       WHERE relationships.rel_id = n.id""", [[c[0] for c in changes], [c[1] for c in changes]])
    return len(changes)
