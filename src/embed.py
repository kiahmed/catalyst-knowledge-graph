"""Card embeddings for semantic + filtered (hybrid) search.

One vector per card, stored on its Firestore doc (CKG-<sector>/catalysts/
items/{card_id}.embedding) next to the flat filter fields Firestore's vector
pre-filter needs (entity_names, rel_types). Firestore only stores and indexes
vectors — we compute them: here, for the backfill (`make embed`) and ingest.

Text per card (what the vector "means"): headline, finding, every live
relationship as an active-voice sentence (so the arrow's direction is part
of the meaning — "Hyundai Motor Group deploys products of Boston Dynamics"),
its mechanism, and the corporate family of the companies involved
("Hyundai Rotem is a subsidiary of Hyundai Motor Group") so a bare
"Hyundai" query still lands on the right cards.

Model gemini-embedding-2 at 768 dimensions (auto-normalized; Firestore max
2048). It takes task prefixes instead of a task_type: documents are
"title: … | text: …", queries "task: search result | query: …".
"""
from __future__ import annotations

import hashlib
from typing import Any

import duckdb

MODEL = "gemini-embedding-2"
DIMENSIONS = 768
_BATCH = 50             # cards per embed call (each wrapped in its own Content)
_MAX_CHARS = 20000      # well under the 8,192-token input limit

# Active-voice phrasing per relationship type (direction rules:
# config/relationship_vocab.yaml).
_VERB = {
    "partners_with": "partners with", "acquires": "acquires", "invests_in": "invests in",
    "supplies": "supplies", "integrates_with": "integrates with", "competes_with": "competes with",
    "displaces": "is displacing", "deploys": "deploys (as a customer) products of",
    "pilots": "pilots (as a customer) products of", "hires_from": "hires talent from",
    "spins_out_from": "is a spin-out of", "built_on": "is built on",
    "benchmarks_against": "benchmarks against", "regulates": "regulates",
    "litigates_against": "is in litigation against",
}


def card_inputs(con: duckdb.DuckDBPyConnection, sector: str,
                entry_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """entry_id → {"text", "hash", "entity_names", "rel_types"} for live cards."""
    where, params = ["c.sector = ?"], [sector]
    if entry_ids is not None:
        if not entry_ids:
            return {}
        where.append(f"c.entry_id IN ({','.join('?' * len(entry_ids))})")
        params += entry_ids
    cards = con.execute(f"""SELECT catalyst_id, entry_id, headline, raw_finding, timestamp
                            FROM catalysts c WHERE {' AND '.join(where)}""", params).fetchall()
    # Every family line per entity (a company can have a parent AND sisters),
    # in a fixed order so the text — and its hash — never depends on scan order.
    family: dict[str, list[str]] = {}
    for child, rel, par in con.execute("""
            SELECT ec.name, l.relation, ep.name FROM entity_links l
            JOIN entities ec ON ec.entity_id = l.child_id JOIN entities ep ON ep.entity_id = l.parent_id
            ORDER BY ec.name, l.relation, ep.name""").fetchall():
        line = f"{child} is a subsidiary of {par}." if rel == "subsidiary_of" else f"{child} and {par} share an owner."
        family.setdefault(child, []).append(line)
        if rel == "sister_of":
            family.setdefault(par, []).append(line)
    out = {}
    for cid, eid, headline, finding, ts in cards:
        rels = con.execute("""
            SELECT ea.name, r.rel_type, eb.name, r.mechanism FROM relationships r
            JOIN entities ea ON ea.entity_id = r.entity_a_id JOIN entities eb ON eb.entity_id = r.entity_b_id
            WHERE r.catalyst_id = ? AND r.status IN ('active', 'materialized') AND r.entity_a_id <> r.entity_b_id
            ORDER BY r.confidence DESC, r.rel_id""", [cid]).fetchall()
        names = sorted({n for a, _t, b, _m in rels for n in (a, b)})
        lines = [f"- {a} {_VERB.get(t, t.replace('_', ' '))} {b}." + (f" {m}" if m else "") for a, t, b, m in rels]
        fam = sorted({line for n in names for line in family.get(n, [])})
        body = f"{finding}\n\nRelationships:\n" + "\n".join(lines) + \
            ("\n\nCorporate structure:\n" + "\n".join(fam) if fam else "")
        text = f"title: {headline} | text: {body}"[:_MAX_CHARS]
        out[eid] = {"text": text, "hash": hashlib.sha256(f"{MODEL}:{DIMENSIONS}:{text}".encode()).hexdigest(),
                    "entity_names": names, "rel_types": sorted({t for _a, t, _b, _m in rels}),
                    "date": ts.isoformat() if ts else None}
    return out


def embed_texts(client: Any, texts: list[str]) -> list[list[float]]:
    """One vector per text (each input wrapped in its own Content so
    gemini-embedding-2 doesn't aggregate them)."""
    from google.genai import types  # noqa: PLC0415

    from .retry import call_with_retry  # noqa: PLC0415

    vectors: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        chunk = texts[i:i + _BATCH]
        resp = call_with_retry(
            lambda: client.models.embed_content(
                model=MODEL,
                contents=[types.Content(parts=[types.Part.from_text(text=t)]) for t in chunk],
                config=types.EmbedContentConfig(output_dimensionality=DIMENSIONS)),
            op="embed", context={"n": len(chunk)})
        if len(resp.embeddings) != len(chunk):
            raise RuntimeError(f"embed: asked for {len(chunk)} vectors, got {len(resp.embeddings)}")
        # A wrong-size vector would be saved with a "current" hash yet never
        # match the 768-dim index — silently and permanently unsearchable.
        bad = [i for i, e in enumerate(resp.embeddings) if not e.values or len(e.values) != DIMENSIONS]
        if bad:
            raise RuntimeError(f"embed: {len(bad)} vector(s) not {DIMENSIONS}-dimensional (first at {bad[0]})")
        vectors += [list(e.values) for e in resp.embeddings]
    return vectors


def query_text(q: str) -> str:
    return f"task: search result | query: {q}"
