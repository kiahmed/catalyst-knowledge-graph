"""Supply-chain layers — role layers + on-demand AI healing (technical_spec.md §2.9c part 3).

A company's layer is the ROLE it plays in our catalysts (OpenAI → capital when it
mostly funds robot makers), not a general industry label. The browser derives it
from relationship roles (frontend/assets/graph.js computeLayerDetail); this
module MIRRORS that scoring server-side (keep identical, like heat.py ↔
computeHeat) so `make layers-heal` can find the companies roles can't place.

Healing asks the LLM only about (a) companies with no role layer ("other") and
(b) doubtful ones (top-2 role scores within 20%), judged FROM OUR OWN catalyst
context — the company's live links and its cards' headlines — never general
knowledge. Answers land in `entity_layers`; export ships them as
`layout.layer_overrides {name: layer}`; the browser prefers them over roles.
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb

log = logging.getLogger(__name__)

LAYERS = ("capital", "infra", "maker", "deployer", "government", "other")
AI_LAYERS = ("capital", "infra", "maker", "deployer", "government", "unknown")
# rel_type → ((layer for A, weight), (layer for B, weight)); None = no role. = graph.js LAYER_ROLES
ROLES: dict[str, tuple[tuple[str, float] | None, tuple[str, float] | None]] = {
    "invests_in": (("capital", 1.0), ("maker", 0.5)),
    "supplies": (("infra", 1.0), ("maker", 0.6)),
    "built_on": (("maker", 0.6), ("infra", 1.0)),
    "integrates_with": (None, ("infra", 0.5)),
    "deploys": (("deployer", 1.0), ("maker", 1.0)),
    "pilots": (("deployer", 1.0), ("maker", 1.0)),
    "regulates": (("government", 2.0), None),
}
RIVAL_RELS = ("competes_with", "displaces")
DOUBT = 0.8          # second role ≥ 80% of the main one → doubtful
LIVE = "r.status IN ('active', 'materialized') AND r.entity_a_id <> r.entity_b_id"


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS entity_layers (
        entity_id INTEGER PRIMARY KEY, layer VARCHAR NOT NULL, source VARCHAR NOT NULL,
        locked BOOLEAN DEFAULT FALSE, reason VARCHAR, decided_at TIMESTAMP DEFAULT now(),
        edges_at_decision INTEGER)""")


def _pick(scores: dict[str, float]) -> str:
    best = LAYERS[0]
    for layer in LAYERS:                                   # ties → LAYERS order (= graph.js)
        if scores.get(layer, 0) > scores.get(best, 0):
            best = layer
    return best


def role_layers(con: duckdb.DuckDBPyConnection) -> dict[int, dict[str, Any]]:
    """entity_id → {layer, scores, edges}. Same rules as graph.js computeLayerDetail."""
    scores: dict[int, dict[str, float]] = {}
    rivals: dict[int, list[int]] = {}
    edges: dict[int, int] = {}
    for a, rel, b in con.execute(f"SELECT r.entity_a_id, r.rel_type, r.entity_b_id FROM relationships r WHERE {LIVE}").fetchall():
        edges[a] = edges.get(a, 0) + 1
        edges[b] = edges.get(b, 0) + 1
        roles = ROLES.get(rel)
        if roles:
            for eid, role in ((a, roles[0]), (b, roles[1])):
                if role:
                    s = scores.setdefault(eid, {})
                    s[role[0]] = s.get(role[0], 0.0) + role[1]
        elif rel in RIVAL_RELS:
            rivals.setdefault(a, []).append(b)
            rivals.setdefault(b, []).append(a)
    layer = {eid: _pick(s) for eid, s in scores.items()}
    for (eid,) in con.execute("SELECT entity_id FROM entities WHERE type = 'government'").fetchall():
        layer[eid] = "government"
    for _ in range(10):                                    # rivals' majority, fixed point from a snapshot per pass
        snap = dict(layer)
        changed = 0
        for eid, lst in rivals.items():
            if eid in snap:
                continue
            votes: dict[str, int] = {}
            for r in lst:
                lr = snap.get(r)
                if lr and lr != "other":
                    votes[lr] = votes.get(lr, 0) + 1
            if votes:
                layer[eid] = _pick({k: float(v) for k, v in votes.items()})
                changed += 1
        if not changed:
            break
    ids = set(edges) | set(layer)
    return {eid: {"layer": layer.get(eid, "other"), "scores": scores.get(eid, {}), "edges": edges.get(eid, 0)} for eid in ids}


def _doubtful(scores: dict[str, float]) -> bool:
    top = sorted(scores.values(), reverse=True)
    return len(top) > 1 and top[0] > 0 and top[1] >= DOUBT * top[0]


def candidates(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[dict[str, Any]]:
    """Companies worth asking about, most-linked first. Skips locked/admin rows and anything
    already decided unless it has gained live links since (so `unknown` isn't re-asked)."""
    try:                                                   # read-only callers (cleanup checklist) may lack the table
        decided = {eid: (src, locked, n) for eid, src, locked, n in
                   con.execute("SELECT entity_id, source, locked, edges_at_decision FROM entity_layers").fetchall()}
    except duckdb.CatalogException:
        decided = {}
    names = dict(con.execute("SELECT entity_id, name FROM entities").fetchall())
    out = []
    for eid, info in role_layers(con).items():
        why = "no role" if info["layer"] == "other" else ("doubtful" if _doubtful(info["scores"]) else None)
        if not why or eid not in names:
            continue
        prev = decided.get(eid)
        if prev and (prev[1] or prev[0] == "admin" or (prev[2] or 0) >= info["edges"]):
            continue
        out.append({"entity_id": eid, "name": names[eid], "why": why, "role_layer": info["layer"],
                    "scores": info["scores"], "edges": info["edges"]})
    out.sort(key=lambda c: (c["why"] != "doubtful", -c["edges"]))      # doubtful first, then most-linked
    return out[:limit] if limit else out


def _context(con: duckdb.DuckDBPyConnection, eid: int, max_edges: int = 25, max_cards: int = 5) -> str:
    rows = con.execute(f"""
        SELECT ea.name, r.rel_type, eb.name, coalesce(r.mechanism, ''), c.headline, c.timestamp
        FROM relationships r JOIN catalysts c USING (catalyst_id)
        JOIN entities ea ON ea.entity_id = r.entity_a_id JOIN entities eb ON eb.entity_id = r.entity_b_id
        WHERE {LIVE} AND ? IN (r.entity_a_id, r.entity_b_id)
        ORDER BY c.timestamp DESC LIMIT {int(max_edges)}""", [eid]).fetchall()
    links = [f"- {a} {rel} {b}: {mech[:160]}" for a, rel, b, mech, _h, _t in rows]
    heads, seen = [], set()
    for *_x, h, _t in rows:
        if h and h not in seen and len(heads) < max_cards:
            seen.add(h)
            heads.append(f"- {h}")
    return "Links:\n" + "\n".join(links) + "\nNews headlines:\n" + "\n".join(heads)


_PROMPT = """You place companies in the robotics supply chain by the ROLE each one PLAYS in the
catalysts below — judge only from these links and headlines, not from what the company is
generally known for (a model maker that mostly funds robot startups here is "capital").

Roles:
- capital: funds or buys other companies (investors, VCs, corporates investing).
- infra: supplies chips, components, sensors, AI models or software platforms others build on.
- maker: builds robots or robot AI products that others buy, deploy, pilot or fund.
- deployer: buys, deploys or pilots robots in its own operations (factories, warehouses, hospitals).
- government: regulator, ministry or public body.
- unknown: these links don't show a role (e.g. only rivalry or partnership).

Pick the role most of its links show. One answer per company id.

{companies}"""


def heal(con: duckdb.DuckDBPyConnection, client: Any, cfg: Any, *, limit: int = 200,
         dry_run: bool = False, batch: int = 10) -> dict[str, Any]:
    """Ask the LLM about up to `limit` candidates; write entity_layers (source 'ai').
    dry_run → candidates only, no LLM, no writes."""
    from pydantic import BaseModel, Field  # noqa: PLC0415

    from .extract import _gen_config  # noqa: PLC0415
    from .retry import call_with_retry  # noqa: PLC0415

    if not dry_run:
        ensure_schema(con)
    cands = candidates(con, limit)
    summary: dict[str, Any] = {"candidates": len(cands),
                               "no_role": sum(c["why"] == "no role" for c in cands),
                               "doubtful": sum(c["why"] == "doubtful" for c in cands)}
    if dry_run or not cands:
        summary["sample"] = [{k: c[k] for k in ("name", "why", "role_layer", "edges")} for c in cands[:25]]
        return summary

    class Decision(BaseModel):
        entity_id: int
        layer: str = Field(description="One of: " + ", ".join(AI_LAYERS))
        reason: str = Field(min_length=3, max_length=240)

    class Decisions(BaseModel):
        decisions: list[Decision]

    model = cfg.extraction.model
    counts: dict[str, int] = {}
    written = 0
    for i in range(0, len(cands), batch):
        chunk = cands[i:i + batch]
        blocks = "\n\n".join(f"### id {c['entity_id']}: {c['name']}\n{_context(con, c['entity_id'])}" for c in chunk)
        resp = call_with_retry(
            lambda blocks=blocks: client.models.generate_content(
                model=model, contents=_PROMPT.format(companies=blocks),
                config=_gen_config(model, cfg.extraction.direction_thinking_level, temperature=0.0,
                                   max_output_tokens=8192, response_mime_type="application/json",
                                   response_schema=Decisions)),
            op="layers_heal", context={"companies": len(chunk)})
        parsed = resp.parsed
        if parsed is None:
            log.warning("layers_heal: unparseable batch at %d (%r)", i, (resp.text or "")[:150])
            continue
        by_id = {d.entity_id: d for d in parsed.decisions}
        for c in chunk:
            d = by_id.get(c["entity_id"])
            layer = d.layer.strip().lower() if d else "unknown"
            if layer not in AI_LAYERS:
                layer = "unknown"
            con.execute("""INSERT OR REPLACE INTO entity_layers
                           (entity_id, layer, source, locked, reason, decided_at, edges_at_decision)
                           VALUES (?, ?, 'ai', FALSE, ?, now(), ?)""",
                        [c["entity_id"], layer, (d.reason if d else "no answer")[:240], c["edges"]])
            counts[layer] = counts.get(layer, 0) + 1
            written += 1
    summary.update({"written": written, "by_layer": counts})
    return summary


def overrides(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """{entity name: layer} for the browser; `unknown` decisions are not overrides."""
    try:
        rows = con.execute("""SELECT e.name, l.layer FROM entity_layers l JOIN entities e USING (entity_id)
                              WHERE l.layer <> 'unknown' ORDER BY e.name""").fetchall()
    except duckdb.CatalogException:
        return {}
    return {name: layer for name, layer in rows}
