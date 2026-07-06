"""Extraction — inference-first, two-pass on Gemini 2.5 Flash.

Single public entry point: `extract(entry, config)` → ExtractionResult.

Why two passes (see docs/technical_spec.md §2.2):
  Gemini 2.5 Flash cannot combine `response_schema` with `tools=[google_search]`
  in one call — that combination is Gemini-3-preview only. We run two calls:

    Pass 1 — enrichment (conditional):
      A) If entry.source_url present AND cfg.extraction.url_fetch.enabled:
         fetch the article via src/fetch.py (httpx + trafilatura).
      B) Else if cfg.extraction.grounding.when triggers:
         `tools=[google_search]`, no schema — returns free text + citations.
      C) Neither: extract over raw finding only.

    Pass 2 — extraction (always):
      `response_schema=ExtractionSchema`, no tools. Contents include the
      original entry plus any Pass-1 enrichment text, with citations if any.

Callers: tools/robotics-ingest/main.py, dev-utils/reextract.py, dev-utils/backtest.py.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from .config import RoboticsConfig
from .fetch import FetchResult, fetch_article
from .retry import call_with_retry

log = logging.getLogger(__name__)

# Upper bound on max_output_tokens after a MAX_TOKENS bump. Chosen to be
# generous but still bounded; Gemini 2.5 Flash supports up to 65k.
_MAX_TOKENS_HARD_CAP = 32768


# ── Response schema (Pydantic → Gemini response_schema) ─────────────

EntityType = Literal["public_company", "private_company", "government", "organization"]
SentimentLabel = Literal["Very Bullish", "Bullish", "Neutral", "Bearish", "Very Bearish"]
EvidenceType = Literal["direct", "inferred", "web_grounded", "speculative"]


class EntityOut(BaseModel):
    mention: str = Field(description="Entity name as it appeared in the finding or enrichment text.")
    resolved: str = Field(description="Canonical form (strip Inc./Corp., expand abbreviations).")
    ticker: str | None = Field(default=None, description="Ticker if publicly traded, else null.")
    type: EntityType


class RelationshipOut(BaseModel):
    entity_a: str = Field(description="Canonical name; MUST match an entities[].resolved value.")
    rel_type: str = Field(description="One of the 15 closed-vocab types listed in the prompt.")
    entity_b: str = Field(description="Canonical name; MUST match an entities[].resolved value.")
    evidence_type: EvidenceType = Field(
        description=(
            "direct: stated/obviously implied in the finding. "
            "inferred: reasoned from market structure. "
            "web_grounded: inferred AND supported by Pass-1 citations. "
            "speculative: plausible mechanism but magnitude unclear."
        )
    )
    mechanism: str = Field(
        min_length=15,
        max_length=400,
        description=(
            "1-2 sentences: WHY this edge exists. Be specific. "
            "'AMD Ryzen AI MAX+ 395 undercuts Jetson Thor on perf/$ for warehouse robotics' "
            "beats 'competitive pressure'."
        ),
    )
    mechanism_strength: float = Field(
        ge=0.0, le=1.0,
        description="0..1 — how clear is the causal chain? 1.0 = signed deal / obvious structural fit.",
    )
    impact_magnitude: float = Field(
        ge=0.0, le=1.0,
        description=(
            "0..1 — materiality IF the mechanism plays out. "
            "1.0 = changes sector leader / reshapes a supply chain; "
            "0.3 = marginal share shift."
        ),
    )
    source_refs: list[int] = Field(
        default_factory=list,
        description=(
            "1-based indices into the enrichment's 'Cited sources' list that "
            "support this edge. Populate ONLY when the prompt tells you to. "
            "Leave empty [] if the mechanism rests on training-data priors "
            "rather than on a cited source."
        ),
    )


class ExtractionSchema(BaseModel):
    reasoning: str = Field(
        description=(
            "Think out loud FIRST. What is the finding saying? Which entities matter? "
            "Which edges are DIRECT (stated) and which are INFERRED dotted-lines "
            "(affected companies not named in the finding)? Name the second-order "
            "impact explicitly. 4-8 sentences. Only then commit to the fields below."
        )
    )
    significance_score: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Catalyst-level importance 0..1. Low = filler funding-round / minor product update "
            "with no structural implications. High = sector-shifting news "
            "(major platform launch, M&A, regulatory action, breakthrough benchmark). "
            "If significance < the threshold given in the user prompt, "
            "emit ONLY evidence_type='direct' edges and skip inferred/speculative."
        )
    )
    entities: list[EntityOut]
    relationships: list[RelationshipOut]
    sentiment_label: SentimentLabel = Field(
        description="Copy from the final 'Sentiment:' line of sentiment_takeaways. Do not re-derive."
    )
    headline: str = Field(
        min_length=10, max_length=140,
        description="8-12 words, active voice, no tickers/dates.",
    )


# ── Public data types ───────────────────────────────────────────────


@dataclass
class ExtractionResult:
    entry_id: str
    prompt_version: str
    model: str
    reasoning: str
    significance_score: float
    entities: list[dict[str, Any]]
    relationships: list[dict[str, Any]]     # kept triples (with derived confidence + flagged)
    sentiment_label: str
    headline: str
    research_sources: list[str]             # citation URIs from Pass 1 grounding, if any
    dropped_low_confidence: list[dict[str, Any]]
    flagged_medium_confidence: list[dict[str, Any]]
    enrichment: dict[str, Any] = field(default_factory=dict)  # diagnostic: which path, text len, errs
    raw_response_json: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ── Prompt construction ─────────────────────────────────────────────


def _format_vocab(vocab_yaml: dict[str, Any]) -> str:
    lines = []
    for item in vocab_yaml.get("vocabulary", []):
        name = item["name"]
        desc = " ".join(item["description"].split())
        direction = "DIRECTIONAL (a→b)" if item.get("directional") else "symmetric"
        lines.append(f"- **{name}** [{direction}]: {desc}")
        for ex in item.get("examples", [])[:1]:
            lines.append(f"    e.g. {ex!r}")
    return "\n".join(lines)


_SYSTEM_PROMPT_TEMPLATE = """\
You extract a knowledge graph of DIRECT and INFERRED relationships from Robotics-sector market-research findings.

## Product intent (read carefully)

The Robotics module's value is NOT in restating the obvious edge named in the finding.
The real value is in the **second- and third-order dotted lines** that
existing readers miss — the companies NOT named in the article that are
structurally affected. Examples:

  - "AMD announces Ryzen AI MAX+ 395 robotics platform" → NVIDIA
    (Jetson Thor customers threatened), and Jetson's warehouse-robotics
    deployers (Symbotic, Boston Dynamics) have a new supplier option.
  - "Zhipu releases GLM-4.7, matches GPT-5 on SWE-bench" → OpenAI
    (commercial book erosion), and GLM-deploying platforms get a
    cheaper substrate.

You must emit BOTH kinds:
  - **direct** edges — what the finding explicitly states
  - **inferred** edges — second-order impact you reason about from
    market structure and known existing relationships
  - **web_grounded** edges — inferred AND supported by a cited URL
    in the enrichment text (if Pass 1 grounding ran)
  - **speculative** edges — plausible mechanism, magnitude unclear;
    mark this when you want to flag but not assert

## The significance gate

The user prompt will tell you a threshold (e.g. 0.5). Assign
`significance_score` based on the catalyst's structural importance:

  - 0.0-0.3: minor funding round, incremental product tick, non-news
  - 0.3-0.5: notable but contained (single-customer deal, small M&A)
  - 0.5-0.7: sector-relevant (platform launch, mid-size acquisition)
  - 0.7-1.0: sector-shifting (flagship product, >$1B M&A, regulatory)

If your significance_score is BELOW the threshold, emit ONLY edges with
`evidence_type = "direct"`. Skip all inferred / speculative edges — the
catalyst isn't important enough to justify dotted-line reasoning.

If significance is AT or ABOVE the threshold, you SHOULD emit 1–4
inferred edges. Don't force it if nothing plausible exists, but this
is where the alpha lives.

## The 15-type closed relationship vocabulary

Every `rel_type` MUST be one of these exactly:

{vocab_block}

## Entity rules

- `mention` = string from the finding/enrichment text.
- `resolved` = canonical (strip Inc./Corp./Group; expand BD → Boston
  Dynamics; standard English casing).
- Include only companies / governments / organizations that appear
  in at least one emitted edge. No people. No ETFs.
- `ticker` only for publicly listed equities.

## Relationship rules

- `entity_a` and `entity_b` must exactly match some `resolved` name.
- DIRECTIONAL types: direction matters.
- Symmetric types (partners_with, competes_with, benchmarks_against):
  alphabetize the endpoints for determinism.
- Don't emit the same triple twice with different rel_types — pick
  the most specific.
- Don't duplicate a triple with different evidence_types — pick the
  strongest evidence available.

## Mechanism × impact (two-dimensional confidence)

Each edge gets TWO numbers instead of one confidence:

- `mechanism_strength` (0..1): How clear is the causal chain?
  1.0 = signed deal, regulatory filing, obvious structural fit.
  0.5 = plausible but requires a few assumptions.
  0.2 = speculative chain requiring multiple leaps.

- `impact_magnitude` (0..1): IF the mechanism plays out, how material?
  1.0 = changes sector leader or reshapes a supply chain.
  0.5 = meaningful share shift.
  0.2 = marginal.

Downstream computes `confidence = sqrt(mechanism_strength × impact_magnitude)`.
So DON'T inflate one dimension to carry the other — a low score on
either axis is the correct signal.

## `mechanism` field is non-negotiable

For every edge, `mechanism` is 1-2 sentences answering WHY the edge
exists. Be specific.
  BAD: "competitive pressure"
  GOOD: "Ryzen AI MAX+ 395 delivers ~50 TOPS at half Jetson Thor's
         unit cost, pulling warehouse-robotics integrators toward AMD
         for 2026 platform refreshes."

## `source_refs` field (traceability)
{source_refs_block}
## Sentiment + headline

- `sentiment_label`: copy verbatim from the final 'Sentiment:' line
  of sentiment_takeaways. Do not re-derive.
- `headline`: 8-12 words, active voice, no tickers/dates.

## Reasoning field

Start with the `reasoning` field. Think out loud: what is the finding
saying? Which entities matter? Which direct edges exist? Which
inferred/dotted-line edges exist? Name the affected companies NOT
mentioned in the finding. 4-8 sentences. Then commit to structured
fields.

prompt_version: {prompt_version}
"""


_SOURCE_REFS_PROMPT_ON = """\
Every edge MUST include `source_refs: list[int]` — 1-based indices into
the 'Cited sources' list in the enrichment block below. Rules:

  - `direct` edges: refs optional (the finding body is implicit source 0;
    leave [] unless a cited URL directly corroborates).
  - `inferred` / `web_grounded` edges: include at least ONE ref when a
    cited source backs any part of the mechanism. Leave [] ONLY if the
    claim rests purely on training-data priors (you'll be flagged).
  - `speculative` edges: refs optional.

Do not fabricate indices. If there are no cited sources, `source_refs = []`.
"""

_SOURCE_REFS_PROMPT_OFF = """\
Leave `source_refs` as an empty list [] for every edge. Per-edge
citation traceability is disabled for this run; we're measuring baseline
quality without forced grounding.
"""


def _build_user_prompt(
    entry: dict[str, Any],
    enrichment_text: str,
    citations: list[str],
    min_sig_for_inference: float,
) -> str:
    enrichment_block = ""
    if enrichment_text:
        cite_line = ""
        if citations:
            # 1-based indexing so the LLM's source_refs values are human-readable.
            cite_line = "\nCited sources (1-based index → URI):\n" + "\n".join(
                f"  [{i}] {u}" for i, u in enumerate(citations[:8], start=1)
            )
        enrichment_block = (
            f"\n\n--- enrichment (article body or grounded research) ---\n"
            f"{enrichment_text.strip()}"
            f"{cite_line}\n"
            f"--- end enrichment ---"
        )

    return f"""\
Extract the knowledge graph from this Arboryx Robotics finding.

Significance threshold for inferred edges: {min_sig_for_inference}
  (If your significance_score falls below this, emit only 'direct' edges.)

entry_id: {entry.get("entry_id")}
timestamp: {entry.get("timestamp")}

finding:
{(entry.get("finding") or "").strip()}

sentiment_takeaways:
{(entry.get("sentiment_takeaways") or "").strip()}

source_url: {entry.get("source_url") or "(none)"}{enrichment_block}

Return the structured JSON now.
"""


# ── Pass 1 — enrichment ─────────────────────────────────────────────


_GROUNDING_PROMPT_TEMPLATE = """\
You are researching a market event for a knowledge-graph extractor.
Return 400-800 words of plain prose (NO JSON, NO markdown headers) that
would help a downstream LLM understand the second-order impact.

Focus on:
  - Which companies / products / supply chains are structurally affected?
  - What existing competitive relationships does this shift?
  - What customer / partner lists are implicated?
  - Any prior press on the same entities that provides context.

Cite sources inline with bracketed URLs when possible. Be concrete about
company names and mechanisms — vague industry commentary is not useful.

Event to research:
  entry_id:  {entry_id}
  timestamp: {ts}
  finding:   {finding}

If the finding is self-contained and has no structural second-order
implications worth researching, return a short 2-sentence note saying so.
"""


def _run_grounding(
    entry: dict[str, Any], cfg: RoboticsConfig, client: genai.Client
) -> tuple[str, list[str]]:
    """Pass-1 grounded search. Returns (research_text, citation_uris). Soft-fails to ('', [])."""
    g = cfg.extraction.grounding
    if not g.enabled:
        return "", []

    prompt = _GROUNDING_PROMPT_TEMPLATE.format(
        entry_id=entry.get("entry_id") or "",
        ts=entry.get("timestamp") or "",
        finding=(entry.get("finding") or "").strip(),
    )
    try:
        resp = call_with_retry(
            lambda: client.models.generate_content(
                model=g.model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                    temperature=0.1,
                    max_output_tokens=2048,
                ),
            ),
            op="grounding",
            context={"entry_id": entry.get("entry_id")},
        )
    except Exception as exc:
        # Pass-1 is best-effort — if it fails after retries, fall back to
        # raw-finding extraction instead of aborting the whole entry.
        log.warning(
            "grounding_fail entry_id=%s err=%s: %s",
            entry.get("entry_id"), type(exc).__name__, exc,
        )
        return "", []

    text = resp.text or ""
    citations: list[str] = []
    try:
        gm = resp.candidates[0].grounding_metadata  # type: ignore[union-attr]
        if gm and gm.grounding_chunks:
            for c in gm.grounding_chunks:
                if c.web and c.web.uri:
                    citations.append(c.web.uri)
    except (AttributeError, IndexError):
        pass

    log.info(
        "grounding_ok entry_id=%s chars=%d citations=%d",
        entry.get("entry_id"), len(text), len(citations),
    )
    return text, citations


def _enrich(
    entry: dict[str, Any], cfg: RoboticsConfig, client: genai.Client
) -> tuple[str, list[str], dict[str, Any]]:
    """Route to URL fetch or grounding based on entry state + config.

    Returns (text, citations, diagnostic). Diagnostic dict records the path
    taken + any error, to surface on backtest output.
    """
    diag: dict[str, Any] = {"path": "none"}
    url = entry.get("source_url") or ""
    uf = cfg.extraction.url_fetch
    g = cfg.extraction.grounding

    # Path A: URL fetch
    if url and uf.enabled:
        r: FetchResult = fetch_article(url, uf)
        diag["path"] = "url_fetch"
        diag["url"] = url
        diag["ok"] = r.ok
        diag["chars"] = len(r.text)
        if not r.ok:
            diag["error"] = r.error
        if r.ok:
            return r.text, [url], diag
        # fall through to grounding if url_fetch failed and grounding.when allows

    # Path B: grounded search
    should_ground = (
        g.enabled
        and (
            g.when == "always"
            or (g.when == "missing_source_url" and not url)
            or (g.when == "missing_source_url" and url and not diag.get("ok", True))
        )
    )
    if should_ground:
        text, cites = _run_grounding(entry, cfg, client)
        diag["path"] = "grounding" if diag["path"] == "none" else diag["path"] + "+grounding"
        diag["grounding_chars"] = len(text)
        diag["grounding_citations"] = len(cites)
        if text:
            return text, cites, diag

    # Path C: raw finding only
    return "", [], diag


# ── Public API ──────────────────────────────────────────────────────


def _compute_confidence(ms: float, im: float) -> float:
    """confidence = geometric mean of mechanism_strength and impact_magnitude."""
    return math.sqrt(max(0.0, ms) * max(0.0, im))


def _finish_reason(response: Any) -> str | None:
    """Extract the finish_reason name from a Gemini response. Null-safe."""
    try:
        fr = response.candidates[0].finish_reason  # type: ignore[union-attr,index]
        return getattr(fr, "name", None) or str(fr)
    except (AttributeError, IndexError, TypeError):
        return None


def _call_extraction_with_budget_bump(
    *,
    client: genai.Client,
    entry_id: str,
    model: str,
    user_prompt: str,
    system_prompt: str,
    temperature: float,
    start_tokens: int,
) -> tuple[Any, int]:
    """Pass-2 schema-constrained call with transient-retry + MAX_TOKENS bump.

    Contract:
      1. First attempt at `start_tokens` (wrapped in call_with_retry for 429/5xx)
      2. If response.parsed is None AND finish_reason is MAX_TOKENS, retry
         ONCE with 2x the budget (capped at _MAX_TOKENS_HARD_CAP). The retry
         is also wrapped in call_with_retry.
      3. Any non-MAX_TOKENS parse failure propagates up — the caller raises.

    Returns (response, tokens_used_for_final_attempt).
    """
    tokens = start_tokens

    def _do_call(tok: int) -> Any:
        return client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=ExtractionSchema,
                temperature=temperature,
                max_output_tokens=tok,
            ),
        )

    response = call_with_retry(
        lambda: _do_call(tokens),
        op="extract",
        context={"entry_id": entry_id, "max_output_tokens": tokens},
    )

    if response.parsed is not None:
        return response, tokens

    # Parse failed. Was it a token-budget truncation? If so, bump once.
    reason = _finish_reason(response)
    if reason and "MAX_TOKENS" in reason.upper() and tokens < _MAX_TOKENS_HARD_CAP:
        bumped = min(_MAX_TOKENS_HARD_CAP, tokens * 2)
        log.warning(
            "extract_max_tokens_bump entry_id=%s from=%d to=%d reason=%s",
            entry_id, tokens, bumped, reason,
        )
        response = call_with_retry(
            lambda: _do_call(bumped),
            op="extract_bumped",
            context={"entry_id": entry_id, "max_output_tokens": bumped},
        )
        tokens = bumped

    return response, tokens


def extract(entry: dict[str, Any], cfg: RoboticsConfig) -> ExtractionResult:
    """Run two-pass extraction on one master-log entry."""
    if not cfg.extraction.api_key:
        raise RuntimeError("GEMINI_API_KEY is not set; cannot call Gemini.")

    client = genai.Client(api_key=cfg.extraction.api_key)

    # Pass 1 — enrichment (may return empty)
    enrich_text, citations, enrich_diag = _enrich(entry, cfg, client)

    # Pass 2 — schema-constrained extraction
    source_refs_block = (
        _SOURCE_REFS_PROMPT_ON if cfg.extraction.source_refs.enabled
        else _SOURCE_REFS_PROMPT_OFF
    )
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        vocab_block=_format_vocab(cfg.vocab),
        source_refs_block=source_refs_block,
        prompt_version=cfg.extraction.prompt_version,
    )
    user_prompt = _build_user_prompt(
        entry, enrich_text, citations, cfg.extraction.significance_min_for_inference,
    )

    response, used_tokens = _call_extraction_with_budget_bump(
        client=client,
        entry_id=entry.get("entry_id") or "",
        model=cfg.extraction.model,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=cfg.extraction.temperature,
        start_tokens=cfg.extraction.max_output_tokens,
    )

    parsed: ExtractionSchema | None = response.parsed  # type: ignore[assignment]
    if parsed is None:
        finish_reason = _finish_reason(response)
        raise RuntimeError(
            f"Gemini returned no parseable structured output for {entry.get('entry_id')} "
            f"(finish_reason={finish_reason}, tokens={used_tokens}). "
            f"Raw text: {(response.text or '')[:500]!r}"
        )

    # Enforce the significance gate defensively — if the LLM emitted inferred
    # edges despite low significance, strip them.
    min_sig = cfg.extraction.significance_min_for_inference
    if parsed.significance_score < min_sig:
        parsed.relationships = [
            r for r in parsed.relationships if r.evidence_type == "direct"
        ]

    # Downgrade inferred edges with empty source_refs to speculative when the
    # toggle is on. Keeps 'inferred' a higher-bar label tied to citations.
    sr_cfg = cfg.extraction.source_refs
    if sr_cfg.enabled and sr_cfg.downgrade_inferred_without_refs:
        n_cites = len(citations)
        for rel in parsed.relationships:
            if rel.evidence_type == "inferred" and not rel.source_refs and n_cites > 0:
                rel.evidence_type = "speculative"
            # Validate refs are within range; drop out-of-range refs silently.
            rel.source_refs = [i for i in rel.source_refs if 1 <= i <= n_cites]

    # Partition by confidence band.
    drop_t = cfg.extraction.confidence_drop
    write_t = cfg.extraction.confidence_write
    valid_rel_types = {v["name"] for v in cfg.vocab.get("vocabulary", [])}
    resolved_names = {e.resolved for e in parsed.entities}

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for rel in parsed.relationships:
        r = rel.model_dump()
        r["confidence"] = _compute_confidence(rel.mechanism_strength, rel.impact_magnitude)
        # Hard validation first.
        if rel.rel_type not in valid_rel_types:
            r["_drop_reason"] = f"unknown rel_type {rel.rel_type!r}"
            dropped.append(r)
            continue
        if rel.entity_a not in resolved_names or rel.entity_b not in resolved_names:
            r["_drop_reason"] = "entity_a or entity_b not in entities[]"
            dropped.append(r)
            continue
        # Confidence bands.
        if r["confidence"] < drop_t:
            r["_drop_reason"] = f"derived confidence {r['confidence']:.2f} < drop {drop_t}"
            dropped.append(r)
            continue
        # Speculative ALWAYS flagged regardless of confidence.
        r["flagged"] = rel.evidence_type == "speculative" or r["confidence"] < write_t
        if r["flagged"]:
            flagged.append(r)
        kept.append(r)

    return ExtractionResult(
        entry_id=str(entry.get("entry_id") or ""),
        prompt_version=cfg.extraction.prompt_version,
        model=cfg.extraction.model,
        reasoning=parsed.reasoning,
        significance_score=parsed.significance_score,
        entities=[e.model_dump() for e in parsed.entities],
        relationships=kept,
        sentiment_label=parsed.sentiment_label,
        headline=parsed.headline,
        research_sources=citations,
        dropped_low_confidence=dropped,
        flagged_medium_confidence=flagged,
        enrichment=enrich_diag,
        raw_response_json=response.text or "",
    )


def research_sources_json(result: ExtractionResult) -> str:
    """JSON-encode the citations list for storage in catalysts.research_sources."""
    return json.dumps(result.research_sources, ensure_ascii=False) if result.research_sources else ""
