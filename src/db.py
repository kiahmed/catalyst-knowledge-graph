"""DuckDB schema + low-level writers.

Pure DDL/DML. No LLM calls, no GCS, no business logic beyond the schema
itself. Higher-level orchestration (extract → resolve → write) lives in
src/ingest.py.

Schema mirrors docs/technical_spec.md §2.4 with two additions:
  - ingestion_meta: compound watermark (last_date + last_id) + last_gcs_gen
  - catalysts.prompt_version: which extractor version populated this row
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb


_SCHEMA_SQL = """
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

-- ── Catalysts — one per Arboryx entry ──────────────────────────
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

-- ── Social handles (shared cache, per canonical entity NOT per entry) ──
-- Verify-or-abstain results per (entity name_key, channel). handle NULL =
-- abstained; source 'blocked' = fetch authwalled, retryable. See
-- docs/handle-resolution-spec.md and src/handles.py.
CREATE TABLE IF NOT EXISTS entity_handles (
    name_key     VARCHAR NOT NULL,   -- lower/trimmed entities.name
    channel      VARCHAR NOT NULL,   -- 'linkedin' | 'x' | future ('reddit', ...)
    handle       VARCHAR,            -- e.g. '@figure-ai', NULL = abstained
    confidence   REAL,
    source       VARCHAR NOT NULL,   -- 'linkedin_verified'|'human'|'abstain'|'blocked'
    comment      VARCHAR,            -- why unresolved / what matched
    resolved_at  TIMESTAMP DEFAULT now(),
    PRIMARY KEY (name_key, channel)
);

-- ── Ingestion watermark ────────────────────────────────────────
-- Compound watermark: (last_processed_date, last_processed_entry_id) is the
-- canonical total-order per spec §2.2. last_gcs_generation short-circuits
-- the GCS download when upstream hasn't changed.
CREATE TABLE IF NOT EXISTS ingestion_meta (
    sector                    VARCHAR PRIMARY KEY,
    last_processed_date       DATE,
    last_processed_entry_id   VARCHAR,
    last_gcs_generation       BIGINT,
    last_processed_at         TIMESTAMP DEFAULT now()
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
"""


def connect(path: str) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection. Caller is responsible for closing."""
    return duckdb.connect(path)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create tables, sequences, indexes. Idempotent."""
    con.execute(_SCHEMA_SQL)
    # entity_handles.comment landed after the table shipped; CREATE TABLE IF
    # NOT EXISTS won't add columns to an existing DB.
    con.execute("ALTER TABLE entity_handles ADD COLUMN IF NOT EXISTS comment VARCHAR")


# ── Watermark ──────────────────────────────────────────────────────


def get_watermark(
    con: duckdb.DuckDBPyConnection, sector: str
) -> tuple[date | None, str, int | None]:
    """Return (last_date, last_entry_id, last_gcs_generation). Empty strings / None if no row yet."""
    row = con.execute(
        """SELECT last_processed_date, last_processed_entry_id, last_gcs_generation
           FROM ingestion_meta WHERE sector = ?""",
        [sector],
    ).fetchone()
    if not row:
        return None, "", None
    return row[0], row[1] or "", row[2]


def set_watermark(
    con: duckdb.DuckDBPyConnection,
    sector: str,
    last_date: date | None,
    last_entry_id: str,
    last_gcs_gen: int | None,
) -> None:
    con.execute(
        """
        INSERT INTO ingestion_meta (sector, last_processed_date, last_processed_entry_id,
                                    last_gcs_generation, last_processed_at)
        VALUES (?, ?, ?, ?, now())
        ON CONFLICT (sector) DO UPDATE SET
            last_processed_date = excluded.last_processed_date,
            last_processed_entry_id = excluded.last_processed_entry_id,
            last_gcs_generation = excluded.last_gcs_generation,
            last_processed_at = now()
        """,
        [sector, last_date, last_entry_id, last_gcs_gen],
    )


def get_processed_entry_ids(con: duckdb.DuckDBPyConnection, sector: str) -> set[str]:
    """Defense-in-depth against duplicate processing, regardless of file order."""
    rows = con.execute(
        "SELECT entry_id FROM catalysts WHERE sector = ?", [sector]
    ).fetchall()
    return {r[0] for r in rows}


# ── Catalyst rows ──────────────────────────────────────────────────


def insert_catalyst(
    con: duckdb.DuckDBPyConnection,
    entry: dict[str, Any],
    headline: str,
    sentiment_label: str,
    prompt_version: str,
    significance_score: float | None = None,
    research_sources: str | None = None,
) -> int:
    """Insert catalyst row. Returns the generated catalyst_id.

    research_sources: JSON-encoded list of URIs from grounded search (nullable).
    significance_score: 0..1 catalyst-level impact rating (see spec §2.2).
    """
    row = con.execute(
        """
        INSERT INTO catalysts (entry_id, sector, timestamp, source_url, raw_finding,
                               headline, sentiment_label, sentiment_takeaways,
                               guidance_play, price_levels, prompt_version,
                               significance_score, research_sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING catalyst_id
        """,
        [
            entry.get("entry_id"),
            entry.get("category"),
            entry.get("timestamp"),
            entry.get("source_url"),
            entry.get("finding") or "",
            headline,
            sentiment_label,
            entry.get("sentiment_takeaways"),
            entry.get("guidance_play"),
            entry.get("price_levels"),
            prompt_version,
            significance_score,
            research_sources,
        ],
    ).fetchone()
    assert row is not None
    return int(row[0])


def delete_by_entry_id(con: duckdb.DuckDBPyConnection, entry_id: str) -> int:
    """Delete catalyst + its relationships + its unresolved rows. For reextract.

    Returns number of catalysts deleted (0 or 1).
    """
    row = con.execute(
        "SELECT catalyst_id FROM catalysts WHERE entry_id = ?", [entry_id]
    ).fetchone()
    if not row:
        return 0
    cid = int(row[0])
    con.execute("DELETE FROM relationships WHERE catalyst_id = ?", [cid])
    con.execute("DELETE FROM unresolved_entities WHERE catalyst_id = ?", [cid])
    con.execute("DELETE FROM catalysts WHERE catalyst_id = ?", [cid])
    return 1


def cleanup_orphans(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Defensive pre-flight sweep. Called at the top of run_ingest.

    write_extraction is a single transaction, so DuckDB's ACID guarantees
    already prevent partial rows from being visible. This function catches
    edge cases:
      - unresolved_entities rows whose parent catalyst was deleted via
        delete_by_entry_id (older versions didn't cascade; defensive)
      - relationships whose endpoint entities were hard-deleted out-of-band

    Returns counts per category for logging. Safe to run every boot.
    """
    counts: dict[str, int] = {}

    # unresolved_entities with no parent catalyst
    row = con.execute(
        """SELECT COUNT(*) FROM unresolved_entities
           WHERE catalyst_id NOT IN (SELECT catalyst_id FROM catalysts)"""
    ).fetchone()
    counts["orphan_unresolved_deleted"] = int(row[0]) if row else 0
    con.execute(
        """DELETE FROM unresolved_entities
           WHERE catalyst_id NOT IN (SELECT catalyst_id FROM catalysts)"""
    )

    # relationships with missing catalyst or entity endpoints
    row = con.execute(
        """SELECT COUNT(*) FROM relationships
           WHERE catalyst_id NOT IN (SELECT catalyst_id FROM catalysts)
              OR entity_a_id NOT IN (SELECT entity_id FROM entities)
              OR entity_b_id NOT IN (SELECT entity_id FROM entities)"""
    ).fetchone()
    counts["orphan_relationships_deleted"] = int(row[0]) if row else 0
    con.execute(
        """DELETE FROM relationships
           WHERE catalyst_id NOT IN (SELECT catalyst_id FROM catalysts)
              OR entity_a_id NOT IN (SELECT entity_id FROM entities)
              OR entity_b_id NOT IN (SELECT entity_id FROM entities)"""
    )

    return counts


# ── Entities ───────────────────────────────────────────────────────


def find_entity_exact(
    con: duckdb.DuckDBPyConnection, name: str
) -> int | None:
    """Exact-match lookup on entities.name or entity_aliases.alias (case-insensitive)."""
    row = con.execute(
        "SELECT entity_id FROM entities WHERE LOWER(name) = LOWER(?)", [name]
    ).fetchone()
    if row:
        return int(row[0])
    row = con.execute(
        "SELECT entity_id FROM entity_aliases WHERE LOWER(alias) = LOWER(?) LIMIT 1",
        [name],
    ).fetchone()
    return int(row[0]) if row else None


def all_entity_names(con: duckdb.DuckDBPyConnection) -> list[tuple[int, str]]:
    """All (entity_id, name) pairs — used by fuzzy resolver."""
    rows = con.execute("SELECT entity_id, name FROM entities").fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def insert_entity(
    con: duckdb.DuckDBPyConnection,
    name: str,
    ticker: str | None,
    type_: str,
) -> int:
    row = con.execute(
        """
        INSERT INTO entities (name, ticker, type) VALUES (?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET ticker = COALESCE(excluded.ticker, entities.ticker)
        RETURNING entity_id
        """,
        [name, ticker, type_],
    ).fetchone()
    assert row is not None
    return int(row[0])


def insert_alias(
    con: duckdb.DuckDBPyConnection, entity_id: int, alias: str
) -> None:
    con.execute(
        """
        INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)
        ON CONFLICT (entity_id, alias) DO NOTHING
        """,
        [entity_id, alias],
    )


def insert_unresolved(
    con: duckdb.DuckDBPyConnection,
    mention: str,
    catalyst_id: int,
    suggested_name: str | None,
    suggested_type: str | None,
) -> None:
    con.execute(
        """INSERT INTO unresolved_entities (mention, catalyst_id, suggested_name, suggested_type)
           VALUES (?, ?, ?, ?)""",
        [mention, catalyst_id, suggested_name, suggested_type],
    )


# ── Social handles (shared cache) ──────────────────────────────────


def get_handles(
    con: duckdb.DuckDBPyConnection, name_keys: list[str]
) -> dict[tuple[str, str], tuple[str | None, str]]:
    """(name_key, channel) -> (handle, source) for the given keys."""
    if not name_keys:
        return {}
    placeholders = ", ".join("?" for _ in name_keys)
    rows = con.execute(
        f"SELECT name_key, channel, handle, source FROM entity_handles"
        f" WHERE name_key IN ({placeholders})",
        name_keys,
    ).fetchall()
    return {(k, c): (h, s) for (k, c, h, s) in rows}


def upsert_handle(
    con: duckdb.DuckDBPyConnection,
    name_key: str,
    channel: str,
    handle: str | None,
    confidence: float,
    source: str,
    comment: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO entity_handles (name_key, channel, handle, confidence, source, comment, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (name_key, channel) DO UPDATE SET
            handle = excluded.handle,
            confidence = excluded.confidence,
            source = excluded.source,
            comment = excluded.comment,
            resolved_at = excluded.resolved_at
        """,
        [name_key, channel, handle, confidence, source, comment],
    )


def unresolved_handles_report(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    """Everything without a usable handle: attempted-but-unresolved rows
    (with the why in comment) plus company entities never attempted."""
    rows = con.execute(
        """
        SELECT name_key, channel, source, comment, resolved_at
        FROM entity_handles
        WHERE handle IS NULL
        ORDER BY channel, name_key
        """
    ).fetchall()
    unattempted = [
        r[0] for r in con.execute(
            """
            SELECT e.name FROM entities e
            WHERE LOWER(e.type) LIKE '%company%'
              AND NOT EXISTS (
                SELECT 1 FROM entity_handles h
                WHERE h.name_key = LOWER(TRIM(e.name))
              )
            ORDER BY e.name
            """
        ).fetchall()
    ]
    return {
        "unresolved": [
            {
                "entity": k, "channel": c, "source": s, "comment": m,
                "resolved_at": str(t) if t else None,
            }
            for (k, c, s, m, t) in rows
        ],
        "never_attempted": unattempted,
    }


def entities_missing_handles(
    con: duckdb.DuckDBPyConnection, channel: str, limit: int
) -> list[tuple[str, list[str]]]:
    """(name, aliases) for entities with no entity_handles row for `channel`.

    'blocked' rows count as missing (retryable); 'abstain'/verified do not.
    Only company-ish entities — people/products don't get company pages.
    """
    rows = con.execute(
        """
        SELECT e.name, LIST(a.alias),
               -- blocked rows retry LAST so stuck entities can't starve
               -- never-attempted ones (and can't trip circuit breakers
               -- at the head of every sweep)
               MAX(CASE WHEN h.name_key IS NOT NULL THEN 1 ELSE 0 END) AS was_blocked
        FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id = e.entity_id
        LEFT JOIN entity_handles h
               ON h.name_key = LOWER(TRIM(e.name))
              AND h.channel = ? AND h.source = 'blocked'
        WHERE LOWER(e.type) LIKE '%company%'
          AND NOT EXISTS (
            SELECT 1 FROM entity_handles hx
            WHERE hx.name_key = LOWER(TRIM(e.name))
              AND hx.channel = ?
              AND hx.source != 'blocked'
          )
        GROUP BY e.entity_id, e.name
        ORDER BY was_blocked ASC, e.entity_id
        LIMIT ?
        """,
        [channel, channel, limit],
    ).fetchall()
    return [(name, [a for a in (aliases or []) if a]) for (name, aliases, _b) in rows]


# ── Relationships ──────────────────────────────────────────────────


def insert_relationship(
    con: duckdb.DuckDBPyConnection,
    catalyst_id: int,
    entity_a_id: int,
    rel_type: str,
    entity_b_id: int,
    confidence: float,
    flagged: bool,
    evidence_type: str = "direct",
    mechanism: str | None = None,
    mechanism_strength: float | None = None,
    impact_magnitude: float | None = None,
    first_flagged_at: date | None = None,
    source_refs: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id,
                                   confidence, evidence_type, mechanism,
                                   mechanism_strength, impact_magnitude, flagged,
                                   source_refs, first_flagged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            catalyst_id, entity_a_id, rel_type, entity_b_id,
            confidence, evidence_type, mechanism,
            mechanism_strength, impact_magnitude, flagged,
            source_refs, first_flagged_at,
        ],
    )
