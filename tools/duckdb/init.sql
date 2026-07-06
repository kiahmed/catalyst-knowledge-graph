-- Robotics-module DuckDB schema bootstrap
-- Idempotent: safe to run on an existing database.
-- Canonical spec: docs/technical_spec.md §2.4.
-- Must stay in sync with the _SCHEMA_SQL constant in src/db.py.

-- ── Sequences for auto-incrementing PKs ────────────────────────
CREATE SEQUENCE IF NOT EXISTS seq_entities START 1;
CREATE SEQUENCE IF NOT EXISTS seq_entity_aliases START 1;
CREATE SEQUENCE IF NOT EXISTS seq_catalysts START 1;
CREATE SEQUENCE IF NOT EXISTS seq_relationships START 1;

-- ── Entities ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entities (
    entity_id    INTEGER PRIMARY KEY DEFAULT nextval('seq_entities'),
    name         VARCHAR NOT NULL UNIQUE,
    ticker       VARCHAR,
    type         VARCHAR NOT NULL,
    created_at   TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id     INTEGER PRIMARY KEY DEFAULT nextval('seq_entity_aliases'),
    entity_id    INTEGER NOT NULL REFERENCES entities(entity_id),
    alias        VARCHAR NOT NULL,
    UNIQUE (entity_id, alias)
);

-- ── Catalysts — one per Arboryx entry ────────────────────────
CREATE TABLE IF NOT EXISTS catalysts (
    catalyst_id        INTEGER PRIMARY KEY DEFAULT nextval('seq_catalysts'),
    entry_id           VARCHAR UNIQUE NOT NULL,
    sector             VARCHAR NOT NULL,
    timestamp          DATE NOT NULL,
    source_url         VARCHAR,
    raw_finding        VARCHAR NOT NULL,
    headline           VARCHAR NOT NULL,
    sentiment_label     VARCHAR NOT NULL,
    sentiment_takeaways TEXT,
    guidance_play       TEXT,
    price_levels       TEXT,
    prompt_version     VARCHAR,
    significance_score FLOAT,
    research_sources   TEXT,
    extracted_at       TIMESTAMP DEFAULT now()
);

-- ── Relationships ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS relationships (
    rel_id                       INTEGER PRIMARY KEY DEFAULT nextval('seq_relationships'),
    catalyst_id                  INTEGER NOT NULL REFERENCES catalysts(catalyst_id),
    entity_a_id                  INTEGER NOT NULL REFERENCES entities(entity_id),
    rel_type                     VARCHAR NOT NULL,
    entity_b_id                  INTEGER NOT NULL REFERENCES entities(entity_id),
    confidence                   FLOAT NOT NULL,
    evidence_type                VARCHAR NOT NULL DEFAULT 'direct',
    mechanism                    TEXT,
    mechanism_strength           FLOAT,
    impact_magnitude             FLOAT,
    flagged                      BOOLEAN DEFAULT false,
    source_refs                  TEXT,
    first_flagged_at             DATE,
    materialized_by_catalyst_id  INTEGER,
    invalidated_by_catalyst_id   INTEGER,
    status                       VARCHAR NOT NULL DEFAULT 'active',
    extracted_at                 TIMESTAMP DEFAULT now()
);

-- ── Unresolved entities staging ────────────────────────────────
CREATE TABLE IF NOT EXISTS unresolved_entities (
    mention        VARCHAR NOT NULL,
    catalyst_id    INTEGER NOT NULL,
    suggested_name VARCHAR,
    suggested_type VARCHAR,
    resolved       BOOLEAN DEFAULT false,
    resolved_to    INTEGER,
    created_at     TIMESTAMP DEFAULT now()
);

-- ── Ingestion watermark ────────────────────────────────────────
-- Compound (last_processed_date, last_processed_entry_id) is the canonical
-- total order per spec §2.1. last_gcs_generation short-circuits the GCS
-- download when upstream hasn't changed since the last run.
CREATE TABLE IF NOT EXISTS ingestion_meta (
    sector                    VARCHAR PRIMARY KEY,
    last_processed_date       DATE,
    last_processed_entry_id   VARCHAR,
    last_gcs_generation       BIGINT,
    last_processed_at         TIMESTAMP DEFAULT now()
);

-- ── Social posts (Postiz bookkeeping) ──────────────────────────
CREATE TABLE IF NOT EXISTS social_posts (
    post_id          VARCHAR PRIMARY KEY,
    catalyst_id      INTEGER NOT NULL REFERENCES catalysts(catalyst_id),
    platform         VARCHAR NOT NULL,
    postiz_id        VARCHAR,
    posted_at        TIMESTAMP DEFAULT now(),
    analytics_pulled_at TIMESTAMP,
    impressions      INTEGER,
    engagements      INTEGER,
    clicks           INTEGER
);

-- ── Indexes ────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_catalysts_sector_ts ON catalysts(sector, timestamp);
CREATE INDEX IF NOT EXISTS idx_catalysts_entry_id ON catalysts(entry_id);
CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(rel_type);
CREATE INDEX IF NOT EXISTS idx_relationships_entities ON relationships(entity_a_id, entity_b_id);
CREATE INDEX IF NOT EXISTS idx_relationships_catalyst ON relationships(catalyst_id);
CREATE INDEX IF NOT EXISTS idx_relationships_evidence ON relationships(evidence_type);
CREATE INDEX IF NOT EXISTS idx_relationships_status ON relationships(status);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_lower ON entity_aliases(LOWER(alias));
