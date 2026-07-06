# Claude's Proposal — Catalyst Knowledge Graph

*Response to `next_phase.txt` (user's pivot thesis) and `proposal.md` (market researcher take).
Written with full context of the Arboryx pipeline that already exists.*

> **[SUPERSEDED 2026-04-18]** — This document proposed a "pushed
> weekly markdown digest" as the product surface. That direction was
> later overridden: the product is a **graph UI** (colored nodes,
> edge tooltips showing mechanism, configurable time + weight filters,
> optional invalidated-edge overlay). The insight detectors still
> exist, but they render as graph overlays / highlights, not as a
> markdown document. Canonical direction: `docs/technical_spec.md`
> (especially §2.5 — JSON contract, §2.6 — graph UI, §2.6a — edge
> lifecycle). This file is kept for historical context only.

---

## 1. How realistic is this ask?

**Short answer: the concept is real, the value is real, but the hardest parts are
not the parts the proposal emphasizes.**

Three things need to be true for this pivot to become a product:

1. **Entity resolution has to be correct.** Mapping "NVIDIA", "NVDA", "Nvidia Corp",
   and "Jensen Huang's company" to a single node is the single biggest reason
   graph projects in finance fail. It looks trivial; it isn't. Private companies,
   subsidiaries, brand names, and non-English sources compound the problem.

2. **Relationship extraction has to be precise, not just recall-heavy.** LLMs are
   great at finding "X partnered with Y." They are bad at distinguishing a
   signed commercial agreement from a press-release flirtation, a pilot from
   production, or a sympathy mention from a real dependency. The graph's value
   is destroyed if the edges are noisy.

3. **The graph has to generate an insight that a human analyst would not have
   found themselves in 15 minutes.** This is the actual product test. "Company
   X appeared in 40 catalysts this month" is a vanity metric. "Company X is
   becoming a chokepoint because 6 unrelated large caps all announced
   dependencies on its product in the last 60 days" is a product.

The academic literature confirms the approach is viable — there's active
research on LLM-driven financial knowledge graphs (FinKario, IEEE 2021 paper on
KG-enhanced event extraction, arxiv 2025 work on LLM-driven supply chain KGs).
None of this has shipped as a polished commercial product. The research gap
**is** the market gap.

---

## 2. Where the market researcher's proposal is right

- **The competitive landscape analysis is accurate.** Bloomberg SPLC is lagging
  (built on 10-K filings and customs data). AlphaSense is fundamentally
  search-centric, not graph-centric — even after its Sentieo (2022), Tegus
  ($930M, 2024), and Carousel (2025) acquisitions, it's still a document
  retrieval engine with LLM summarization on top. Dataminr is reactionary and
  trade-oriented, not strategic. Kensho (S&P Global) focuses on event-to-market
  linkage, not relationship topology.
- **The "shadow supply chain" framing is the strongest piece.** Real-time
  relationships from press releases, pilots, and beta partnerships genuinely do
  not show up in Bloomberg SPLC until they hit a 10-K, which can be 12–24
  months late. That is a real arbitrage on *information availability*, not just
  *information speed*.
- **Narrative velocity is a genuinely novel primitive.** Sentiment is
  commoditized. Acceleration of catalyst type (e.g., "Regulatory Approval"
  catalysts jumped 400% in drone delivery in 60 days) is not. This is the
  single most defensible feature in the proposal.
- **The M&A / B2B GTM / credit risk use cases are legitimate buyer segments,
  not hand-waving.** Each one has a real budget line ($25k–$250k/seat/year at
  incumbents).

## 3. Where the market researcher's proposal is weak or misleading

- **It under-weights entity resolution and extraction precision.** The proposal
  jumps from "we already have the ingestion engine" to "now pick Neo4j vs
  ArangoDB." That skips the part where the project lives or dies. Storage
  choice is a weekend decision. Extraction quality is a 6-month decision.
- **It assumes graph queries are the product.** They are not. No analyst wants
  to write Cypher. The product is the *pushed insight* — a weekly digest that
  says "here are 5 relationship changes in Robotics that no one else noticed."
  The graph is the internal machinery.
- **"Democratized graph analytics" as positioning is weak.** This has been
  attempted (Quid, Diffbot, Primer, Yewno, Neo4j's own Bloom) with mixed
  commercial results. "Look, a graph you can query" is not a product — it's a
  technology demo. The pitch needs to be the insight, not the graph.
- **It does not address the cold-start problem.** A graph with 3 months of
  history tells you almost nothing about "narrative velocity" or "who is
  becoming a chokepoint." Those signals need 12–24 months of backfill to be
  credible on day one. The proposal silently assumes the graph is valuable
  from week one. It isn't.
- **It implicitly assumes the user wants to be a B2B SaaS founder.** Arboryx
  today is a research tool for one senior operator. That is a completely
  different build than a multi-tenant, permissioned, SOC2-bearing SaaS. The
  proposal doesn't call out that those are two separate products with very
  different cost curves.

---

## 4. What the real underserved opportunity is

Cutting through both documents:

> **There is no product today that watches a sector continuously, extracts
> the relationships between companies from unstructured daily news, and tells
> an operator when the topology of that sector changes in a way that matters.**

Everything else — supply chain maps, sentiment, earnings call search, event
alerts — is already well-served. The white space is *structural change
detection* driven by a continuously updated entity-relationship graph built
from the open web, not from filings.

The person who benefits from this isn't a day trader. It's:

- A **sector-focused PM or analyst** who is responsible for a thematic book and
  cannot read 200 press releases a day.
- A **corporate strategy team** at a large cap trying to see which startups
  are showing up repeatedly as collaborators of their competitors.
- A **VC partner** in a thematic fund (robotics, defense tech, energy
  transition) who wants to know which private companies are starting to show
  up as nodes connected to public ones.

Note the common thread: **thematic, sector-focused users**. Not generalists.
Arboryx is already sector-focused (6 pipelines). This is not a coincidence —
it is the correct wedge. A generalist "catalyst graph of everything" will
fail; a "robotics relationship radar" can succeed.

---

## 5. MVP — high-level specs only

**Scope one sector end-to-end before touching a second.** The point of the MVP
is to prove the insight exists, not to build infrastructure.

### 5.1 Sector pick

**Robotics.** Rationale:
- Small enough universe (~150–300 relevant public + notable private companies)
  that entity resolution is tractable.
- B2B partnership density is high — lots of pilots, integrations, joint
  ventures hitting the news.
- Less well-covered than crypto or AI, where incumbents already run.
- Arboryx's existing Robotics pipeline already produces structured JSON
  catalyst entries for this sector daily — cold-start is partially solved.

### 5.2 Four-layer architecture

1. **Ingestion (already exists).** Arboryx's Scout → DE → Strategist pipeline
   writes daily JSON entries per sector. No change.
2. **Extraction layer (new).** A post-processing step that takes each finding
   and emits structured triples:
   `(entity_a, relationship_type, entity_b, confidence, source_url, timestamp)`.
   Relationship types are a **closed vocabulary** of ~15 items: `partners_with`,
   `acquires`, `supplies`, `invests_in`, `sues`, `competes_with`, etc. Closed
   vocab is the single most important correctness lever — don't let the LLM
   invent edge types. Entities resolve against a ticker/company master list
   with a fallback "unknown entity" bucket that a human reviews weekly.
3. **Graph storage (new, but trivial).** SQLite or DuckDB for the first 3
   months. Do **not** reach for Neo4j yet. A relational store with a
   `(source, target, type, timestamp, confidence)` edge table handles 99% of
   queries you'll write. Only move to Neo4j when you actually need multi-hop
   traversals that SQL can't express cheaply (which is later than you think).
4. **Insight layer (new).** A nightly job that runs **three** specific
   detectors against the graph:
   - **Chokepoint detector**: entities whose in-degree (as a dependency) grew
     >Nx over a trailing window.
   - **Narrative velocity detector**: per-sector, per-relationship-type,
     moving average of edge creation rate vs. a baseline.
   - **Cluster-break detector**: entity pairs that used to co-occur and
     stopped, or never co-occurred and suddenly do.
   The output of the insight layer is a **human-readable weekly digest** for
   the Robotics sector. That is the product.

### 5.3 What the MVP does *not* include

- No multi-tenant infra. Single-user research tool.
- No web UI. Markdown digest delivered by email or committed to a git repo.
- No real-time alerts. Daily batch is fine and cheaper.
- No private company coverage beyond what Arboryx already picks up
  organically.
- No permissioning, audit logging, SSO, or other SaaS scaffolding.
- No graph query interface for end users.
- Not more than one sector. Robotics only until the insights are proven useful.

### 5.4 Success criteria (before expanding)

Run the MVP for **60 days** against Robotics. It's successful if:

1. The weekly digest surfaces at least **2 relationships per week** that the
   operator didn't already know about from reading their normal sources.
2. At least **1 chokepoint signal per month** that is defensible in hindsight
   (the entity actually was becoming structurally important).
3. The extraction layer achieves **>85% precision** on a hand-labeled sample
   of 200 edges. (Recall can be lower — missed edges are cheap; wrong edges
   are expensive.)

If those three hit, expand to a second sector. If they don't, the graph
pivot is unlikely to become a product, and the finding is still valuable:
it means the signal isn't there, and no amount of Neo4j will create it.

### 5.5 What to build *first*, concretely

The first ticket, scoped to a week:

> **"Extract structured relationship triples from existing Arboryx Robotics
> JSON entries using a closed-vocabulary LLM extractor, store them in DuckDB,
> and write a daily count query."**

That one ticket either works and unblocks everything else, or reveals that
extraction precision is too low — in which case the problem is surfaced on
day 7, not month 6.

---

## 6. TL;DR

- The pivot thesis is sound. The proposal.md analysis of incumbents is
  accurate. The white space is real.
- The hard problems are entity resolution and extraction precision, not
  storage or query language. Proposal.md under-weights both.
- The product is the **pushed insight digest**, not the graph itself. A graph
  you can query is not a product; a weekly "here are 5 things no one else
  noticed" is.
- MVP: **one sector (Robotics), closed-vocab extraction, DuckDB, three
  detectors, weekly markdown digest, 60-day evaluation window.** Do not
  expand scope until this proves the signal exists.
- Arboryx's sector-focused ingestion is already the unfair advantage. Do
  not dilute it with a "catalyst graph of everything" mandate.
