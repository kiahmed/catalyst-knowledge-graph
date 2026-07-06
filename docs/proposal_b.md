# Proposal B — Robotics module: From Arboryx to Product

*Revised proposal incorporating pressure-testing from workbench sessions.
Replaces the original `proposal.md` and `claude_proposal.md` as the working direction.*

> **[UPDATED 2026-04-18]** — Proposal B's core thesis (shadow supply
> chain, second-order edges, closed-vocab graph) is still the working
> direction. Two deltas vs the text below:
>
> 1. **Product surface is a graph UI**, not a pushed markdown digest.
>    Insight detectors render as graph overlays / node highlights; see
>    `docs/technical_spec.md` §2.5–§2.6.
> 2. **Edge lifecycle is event-driven**: new edges trigger LLM
>    validation of existing edges on the same pair (confirm → boost,
>    invalidate → penalty + `status='invalidated'`). See §2.6a.
>
> For anything else, this document remains the product thesis.

---

## 1. What we're building

**The Robotics module** — a live, visual sector intelligence product that surfaces structural changes in thematic sectors before they're consensus. Powered by Arboryx's existing daily AI agent pipeline.

The product is NOT a queryable graph database. It is NOT a news summarizer. It is a system that watches a sector continuously, extracts the relationships between companies from unstructured daily catalysts, and tells users when the topology of that sector changes in a way that matters — packaged as shareable visual artifacts.

---

## 2. Why this, why now

### The asset we already have

Arboryx produces daily, structured, deduplicated intelligence across 6 sectors (Robotics, Crypto, AI Stack, Space & Defense, Power & Energy, Strategic Minerals) via autonomous AI agents on GCP. Each finding follows this schema:

```json
{
  "timestamp": "ISO-8601",
  "category": "Robotics",
  "finding": "free-text catalyst description",
  "sentiment_takeaways": "Direct: ... | Indirect: ... | Sentiment: Bullish",
  "guidance_play": "...",
  "price_levels": "..."
}
```

This is a continuously growing corpus of sector-specific market events. It's hard to replicate — not because the technology is secret, but because the pipeline is deployed, running, and accumulating history daily.

### The gap in the market

- **Bloomberg SPLC** maps supply chains from 10-K filings — 12-24 months late.
- **AlphaSense** is search-centric — you have to know what to look for.
- **Dataminr** is reactionary — events, not structure.
- **Kensho** links events to prices, not to each other.

Nobody watches a sector continuously and tells you when the relationship structure changes. That's the white space.

### Why Robotics first

- Small entity universe (~150-300 companies) — entity resolution is tractable
- High B2B partnership density — lots of edges to extract
- Less covered than crypto/AI by incumbents
- Arboryx already ingests it daily — partial cold-start advantage

---

## 3. Product concept

### Three product surfaces

**A. Catalyst cards** — Each finding becomes a structured, shareable visual card:
```
[ROBOTICS] [PARTNERSHIP]
Boston Dynamics + Hyundai expand warehouse automation JV
Related: AMZN, Symbotic, Berkshire Grey
Catalyst velocity: Robotics partnerships up 40% in 30 days
Published Apr 15 — track outcome →
```
Designed for Twitter/LinkedIn screenshots. The viral mechanic.

**B. Relationship map** — Interactive visual: companies as nodes, catalysts as edges, time slider. Watch a sector evolve. See clusters form, dependencies solidify, new entrants appear. Not a query tool — a "watch this sector breathe" visualization.

**C. Track record** — Every catalyst is timestamped. After 30/60/90 days, show what happened: "This was flagged on day X. Stock moved Y% in Z days." Verifiable signal builds trust and drives word-of-mouth.

### Who it's for (primary wedge)

**Thematic investors and sector operators on FinTwit/LinkedIn** — people who follow robotics (or later: AI, defense, energy) as a theme, trade or invest around it, and share insights to build their audience. They currently piece together intelligence from 20+ sources manually. The Robotics module gives them an edge and a shareable format.

Secondary audiences (later phases): fund analysts, VC deal sourcing, corp dev teams, B2B sales trigger events.

### How they get value

- See partnerships, deals, and dependencies forming before they're widely covered
- Spot companies becoming structural chokepoints in a sector
- Track catalyst velocity — is a sector accelerating or cooling?
- Share timestamped insights that build their credibility when confirmed later

---

## 4. Data architecture

### Current state (Arboryx)

Findings live in Firestore at `findings/{entry_id}` — one document per finding, written transactionally by Arboryx (single source of truth since 2026-05-10). Finding-centric, not entity-centric. No relationship extraction, no entity resolution.

### Target state (Robotics module)

**DuckDB** as the analytical store, indexed by entity and relationship. JSON stays as the raw archive — never deleted, always the source of truth for replay.

#### Schema

```sql
-- Canonical entity registry
CREATE TABLE entities (
    entity_id   INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL,          -- canonical name: "NVIDIA"
    ticker      VARCHAR,                   -- nullable, not all entities are public
    sector      VARCHAR NOT NULL,          -- "Robotics"
    entity_type VARCHAR NOT NULL,          -- public_company | private_company | government | organization
    created_at  TIMESTAMP DEFAULT now()
);

-- Alias table for entity resolution
CREATE TABLE entity_aliases (
    alias       VARCHAR NOT NULL,          -- "NVDA", "Nvidia Corp", "Jensen Huang's company"
    entity_id   INTEGER NOT NULL REFERENCES entities(entity_id),
    PRIMARY KEY (alias, entity_id)
);

-- Raw catalysts imported from Arboryx JSON
CREATE TABLE catalysts (
    catalyst_id     INTEGER PRIMARY KEY,
    timestamp       TIMESTAMP NOT NULL,
    sector          VARCHAR NOT NULL,
    raw_finding     TEXT NOT NULL,
    sentiment       VARCHAR,               -- extracted from sentiment_takeaways
    guidance_play   TEXT,
    price_levels    TEXT,
    source_json     VARCHAR,               -- which JSON file this came from
    imported_at     TIMESTAMP DEFAULT now()
);

-- Extracted relationship triples (the core of the graph)
CREATE TABLE relationships (
    relationship_id INTEGER PRIMARY KEY,
    catalyst_id     INTEGER NOT NULL REFERENCES catalysts(catalyst_id),
    entity_a_id     INTEGER NOT NULL REFERENCES entities(entity_id),
    entity_b_id     INTEGER NOT NULL REFERENCES entities(entity_id),
    rel_type        VARCHAR NOT NULL,      -- from closed vocabulary
    confidence      FLOAT NOT NULL,        -- 0.0 to 1.0
    extracted_at    TIMESTAMP DEFAULT now()
);
```

#### Closed relationship vocabulary (15 types)

| rel_type | Meaning | Example |
|---|---|---|
| `partners_with` | Joint venture, collaboration, co-development | Boston Dynamics + Hyundai JV |
| `supplies` | Vendor/supplier relationship | Lidar Co supplies Waymo |
| `acquires` | Acquisition, merger | Google acquires DeepMind |
| `invests_in` | Funding, equity stake | SoftBank invests in Figure AI |
| `sues` | Litigation, patent dispute | Company A sues Company B |
| `competes_with` | Direct competition stated | Tesla vs BYD in humanoids |
| `licenses_from` | IP/technology licensing | ARM licenses to NVIDIA |
| `integrates_with` | API/platform integration | ROS2 integration by Company X |
| `spins_off` | Subsidiary/division spinoff | Alphabet spins off Intrinsic |
| `hires_from` | Notable talent movement | CTO hired from Boston Dynamics |
| `contracts_with` | Government/enterprise contract | DoD contract with Anduril |
| `co_develops` | Joint R&D, not yet commercial | Co-developing warehouse bots |
| `exits` | Divestiture, exit, shutdown | Exits robotics division |
| `regulates` | Regulatory action, approval, ban | FAA approves drone delivery |
| `depends_on` | Stated dependency on supplier/platform | Depends on NVIDIA Jetson |

This vocabulary is intentionally closed. The LLM extractor must pick from this list — it cannot invent new types. This is the single most important correctness lever.

#### Migration utility

A one-time + incremental migration pipeline:

1. **Read** Arboryx findings from Firestore (`findings/{entry_id}`) by sector
2. **Filter** to Robotics only (Phase 1)
3. **Extract** entities and relationships from each finding using LLM with closed vocab + entity master list
4. **Resolve** entities against the `entities` + `entity_aliases` tables; unresolved entities go to an `unresolved_entities` staging table for human review
5. **Insert** into DuckDB

A single ingest path covers both the first run (empty DB → walks every Robotics finding oldest-first) and daily incremental (compound watermark cursor advances after each successful write).

---

## 5. Validation before building (Phase 0)

Before writing product code, validate the content and audience with zero engineering:

**Content-first test (3-4 weeks):**
- Manually format 2-3 Arboryx Robotics findings per day as structured posts on Twitter/LinkedIn
- Use Canva or simple templates — no app needed
- Measure: who engages (their bios = real audience), what format gets shared, do people DM asking for more, does anyone offer to pay

**What this tells us:**
- Whether the content is interesting enough to share
- Who the actual audience is (may differ from our assumptions)
- Which format resonates (cards vs. threads vs. charts vs. relationship maps)
- Whether there's willingness to pay

**Gate:** proceed to Phase 1 only if engagement demonstrates an audience exists.

---

## 6. Phased build plan

| Phase | Scope | Duration | Gate to next |
|---|---|---|---|
| **0 — Validate** | Manual catalyst posts on Twitter/LinkedIn. No engineering. | 3-4 weeks | Engagement confirms audience + format |
| **1 — Prove the signal** | Robotics only. DuckDB + migration utility + extraction pipeline + catalyst card generator + static site. | 4-6 weeks | Organic shares; ≥2 novel insights/week |
| **2 — Prove the product** | Add relationship map (Robotics only). Track record scoring. Basic user accounts. | 4-6 weeks | Returning users >30% WoW |
| **3 — Prove the expansion** | Add 1 additional sector. Pro tier. Alerts. | 4-6 weeks | Paid conversions from free users |
| **4 — Platform** | Multi-sector. API. Contribution layer. Agent marketplace scaffolding. | Ongoing | Revenue covers infra |

**Rule: nothing from Phase N+1 gets built during Phase N.**

---

## 7. What this is NOT

- Not a queryable graph database (users never see Cypher/SQL)
- Not a general-purpose news aggregator (sector-focused, relationship-focused)
- Not a real-time trading alert system (daily batch, structural insights)
- Not multi-tenant SaaS in Phase 1 (single-operator tool first)
- Not a "graph of everything" (Robotics only until proven)

---

## 8. Success criteria (Phase 1, 60-day window)

1. Extraction layer achieves **>85% precision** on a hand-labeled sample of 200 edges
2. Graph UI surfaces at least **2 non-obvious relationships/week** (inferred or web_grounded edges with mechanism tooltips) that the operator didn't already know
3. At least **1 chokepoint signal/month** that is defensible in hindsight
4. Catalyst cards get **organic shares** on social media (not just from the operator's account)

If these don't hit, the signal isn't there — and no amount of graph infrastructure will create it.
