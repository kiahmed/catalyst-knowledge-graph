"""Direction check for directional relationships (one LLM call per card).

Extraction emits `entity_a --rel_type--> entity_b`; for directional types the
order carries meaning (deploys: A = customer, B = vendor) and Gemini flips
it on passive phrasing ("Atlas is being deployed at Hyundai" → Boston
Dynamics deploys Hyundai). This pass re-reads the card and returns, per
edge, `correct | flip | unclear` under a strict response schema, after
restating the fact in active voice. It judges DIRECTION only — whether an
inferred edge is supported is extraction's call, so nothing is dropped here.

Shared by extract() (Pass 3, new cards) and dev-utils/direction_audit.py
(one-time repair of stored edges). Relationships already on record between
the card's entities are given as context only — the verdict must come from
this card's own text.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Literal

import duckdb
from google import genai
from pydantic import BaseModel, Field

log = logging.getLogger("robotics.direction")

_ARTICLE_CHARS = 6000
_RECORD_LINES = 40


class DirectionVerdict(BaseModel):
    index: int = Field(description="The [n] of the relationship being judged.")
    statement: str = Field(
        min_length=5, max_length=300,
        description="The fact as ONE active-voice sentence: '<subject> <verb> <object>', "
                    "applying the type's direction rule.")
    verdict: Literal["correct", "flip", "unclear"] = Field(
        description="correct: FROM/TO as given match the statement. flip: FROM and TO are swapped "
                    "relative to the statement. unclear: the direction cannot be determined.")
    reason: str = Field(min_length=5, max_length=300, description="Why, citing the item's wording.")


class DirectionCheck(BaseModel):
    verdicts: list[DirectionVerdict]


def directional_types(vocab: dict[str, Any]) -> dict[str, str]:
    """name → one-line rule, for vocab types marked directional."""
    return {v["name"]: " ".join(v["description"].split())
            for v in vocab.get("vocabulary", []) if v.get("directional")}


_PROMPT = """\
You check the DIRECTION of relationships extracted from ONE news item.

Each relationship reads FROM --type--> TO. Direction rules for each type:
{rules}

Passive or indirect wording is the usual trap. "Boston Dynamics' Atlas is being
deployed at Hyundai's plant" means Hyundai (customer) deploys Boston Dynamics
(vendor), so Hyundai Motor Group --deploys--> Boston Dynamics.

## News item
{finding}
{article}
## Already on record between these companies (other cards — context only)
{on_record}

You judge DIRECTION only — not whether the relationship is important or stated
in the item. Many relationships are inferred from market context; treat the
mechanism text as part of the evidence. Decide which company is FROM and which
is TO using the type's rule, the item, the mechanism, and well-established
facts (e.g. Hyundai Motor Group owns Boston Dynamics). The record above may
itself contain mistakes; use it only as a hint.

## Relationships to check
{edges}

For EVERY [n] above: restate the fact as one active-voice sentence using the
type's rule, then give the verdict and a short reason:
- correct: the given FROM/TO match your sentence.
- flip: your sentence has FROM and TO the other way round.
- unclear: you genuinely cannot tell the direction (it stays as extracted).
"""


def check(client: genai.Client, *, model: str, thinking_level: str, finding: str, article: str,
          edges: list[dict[str, Any]], rules: dict[str, str], on_record: list[str]) -> dict[int, DirectionVerdict]:
    """Verdicts keyed by edge index. Raises on API/parse failure — callers decide fail-open."""
    from .extract import _gen_config  # noqa: PLC0415 (shared Gemini 2/3 request config)
    from .retry import call_with_retry  # noqa: PLC0415

    used = sorted({e["rel_type"] for e in edges})
    prompt = _PROMPT.format(
        rules="\n".join(f"- {t}: {rules[t]}" for t in used),
        finding=finding.strip(),
        article=f"\n## Article excerpt\n{article[:_ARTICLE_CHARS]}\n" if article else "",
        on_record="\n".join(on_record[:_RECORD_LINES]) or "(none)",
        edges="\n".join(f"[{i}] {e['entity_a']} --{e['rel_type']}--> {e['entity_b']}"
                        f" | mechanism: {e.get('mechanism') or ''}" for i, e in enumerate(edges)),
    )
    resp = call_with_retry(
        lambda: client.models.generate_content(
            model=model, contents=prompt,
            config=_gen_config(model, thinking_level, temperature=0.0, max_output_tokens=16384,
                               response_mime_type="application/json", response_schema=DirectionCheck)),
        op="direction_check", context={"edges": len(edges)})
    parsed: DirectionCheck | None = resp.parsed  # type: ignore[assignment]
    if parsed is None:
        raise RuntimeError(f"direction_check: no parseable output ({(resp.text or '')[:200]!r})")
    return {v.index: v for v in parsed.verdicts if 0 <= v.index < len(edges)}


def on_record_lookup(con: duckdb.DuckDBPyConnection) -> Callable[[list[str]], list[str]]:
    """Returns names → lines like 'Hyundai Motor Group --acquires--> Boston Dynamics (25 cards)'
    for relationships already stored between any two of those entities. Names
    are matched on canonical name or alias, case-insensitively ("Hyundai" →
    Hyundai Motor Group)."""
    def lookup(names: list[str]) -> list[str]:
        keys = sorted({n.strip().lower() for n in names if n and n.strip()})
        if len(keys) < 2:
            return []
        marks = ",".join("?" * len(keys))
        ids = [r[0] for r in con.execute(f"""
            SELECT entity_id FROM entities WHERE lower(name) IN ({marks})
            UNION SELECT entity_id FROM entity_aliases WHERE lower(alias) IN ({marks})""",
            keys + keys).fetchall()]
        if len(ids) < 2:
            return []
        im = ",".join("?" * len(ids))
        rows = con.execute(f"""
            SELECT ea.name, r.rel_type, eb.name, COUNT(DISTINCT r.catalyst_id) AS n
            FROM relationships r
            JOIN entities ea ON ea.entity_id = r.entity_a_id
            JOIN entities eb ON eb.entity_id = r.entity_b_id
            WHERE r.entity_a_id IN ({im}) AND r.entity_b_id IN ({im}) AND r.entity_a_id <> r.entity_b_id
              AND r.status IN ('active', 'materialized')
            GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT {_RECORD_LINES}""", ids + ids).fetchall()
        return [f"{a} --{rt}--> {b} ({n} card{'s' if n != 1 else ''})" for a, rt, b, n in rows]
    return lookup


def apply_to_extraction(kept: list[dict[str, Any]], verdicts: dict[int, DirectionVerdict],
                        checked: list[int]) -> dict[str, int]:
    """Mutates kept in place: flip swaps entity_a/entity_b. correct, unclear
    and missing verdicts leave the edge as extracted (fail-open)."""
    stats = {"checked": len(checked), "flipped": 0, "unclear": 0, "missing": 0}
    for local_i, kept_i in enumerate(checked):
        v = verdicts.get(local_i)
        if v is None:
            stats["missing"] += 1
            continue
        r = kept[kept_i]
        r["direction_statement"] = v.statement
        r["direction_reason"] = v.reason
        if v.verdict == "flip":
            r["entity_a"], r["entity_b"] = r["entity_b"], r["entity_a"]
            stats["flipped"] += 1
        elif v.verdict == "unclear":
            stats["unclear"] += 1
            r["direction_unclear"] = True       # → review queue once written (ingest.flag_unclear_directions)
    stats["deduped"] = dedupe_kept(kept)
    return stats


def dedupe_kept(kept: list[dict[str, Any]]) -> int:
    """A flip can make two of a card's edges identical (a, rel, b): keep the
    higher-confidence one. Returns how many were removed."""
    best: dict[tuple, int] = {}
    for i, r in enumerate(kept):
        k = (r["entity_a"], r["rel_type"], r["entity_b"])
        if k not in best or (r.get("confidence") or 0) > (kept[best[k]].get("confidence") or 0):
            best[k] = i
    keep = set(best.values())
    removed = len(kept) - len(keep)
    kept[:] = [r for i, r in enumerate(kept) if i in keep]
    return removed


def symmetric_types() -> set[str]:
    """Vocab types where A→B and B→A say the same thing (partners_with, competes_with, benchmarks_against)."""
    from .config import _VOCAB_PATH, _load_yaml  # noqa: PLC0415

    return {v["name"] for v in (_load_yaml(_VOCAB_PATH) or {}).get("vocabulary", []) if not v.get("directional")}


def dedupe_rows(con: duckdb.DuckDBPyConnection, catalyst_ids: list[int] | None = None) -> int:
    """Same for stored rows: per card, one row per (a, rel, b) — the highest
    confidence, then lowest rel_id. Lifecycle refs never point at rel_ids,
    so deleting the extras is safe. Symmetric types count A→B and B→A as the
    same row (per card only). Returns rows deleted."""
    sym = sorted(symmetric_types()) or [""]
    scope = ""
    params: list[Any] = []
    if catalyst_ids is not None:
        if not catalyst_ids:
            return 0
        scope = f"AND catalyst_id IN ({','.join('?' * len(catalyst_ids))})"
        params = list(catalyst_ids)
    doomed = [r[0] for r in con.execute(f"""
        SELECT rel_id FROM (
          SELECT rel_id, ROW_NUMBER() OVER (
                   PARTITION BY catalyst_id, rel_type,
                     CASE WHEN rel_type IN (SELECT UNNEST(?::VARCHAR[])) THEN LEAST(entity_a_id, entity_b_id) ELSE entity_a_id END,
                     CASE WHEN rel_type IN (SELECT UNNEST(?::VARCHAR[])) THEN GREATEST(entity_a_id, entity_b_id) ELSE entity_b_id END
                   ORDER BY confidence DESC, rel_id) AS rn
          FROM relationships WHERE status IN ('active', 'materialized') {scope})
        WHERE rn > 1""", [sym, sym, *params]).fetchall()]
    if doomed:
        con.execute(f"DELETE FROM relationships WHERE rel_id IN ({','.join('?' * len(doomed))})", doomed)
    return len(doomed)
