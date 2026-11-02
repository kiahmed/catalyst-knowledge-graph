"""Post-ingest integrity check — report-only, never mutates.

Born from the 2026-11 Arboryx dedup sweep, which deleted findings upstream
(some wrongly: literal "None" source_urls compared equal). CKG keeps its own
copy of every card, so an upstream delete can't orphan anything here — but
we want to SEE it, and catch any dangling reference our own code introduces.

Checks:
  - DuckDB: relationships → missing catalyst / entity, materialized_by /
    invalidated_by → missing catalyst, aliases → missing entity,
    placeholder source_urls.
  - Arboryx: CKG cards whose finding no longer exists in `findings/`
    (upstream-deleted). Kept on purpose — the card is still true news.
  - Firestore: CKG card docs with no DuckDB row (stale) and vice versa.
"""
from __future__ import annotations

from typing import Any

import duckdb

from .config import RoboticsConfig
from .db import URL_SQL_RE

_SAMPLE = 20

_DB_CHECKS = {
    "rel_missing_catalyst":
        "SELECT r.rel_id FROM relationships r LEFT JOIN catalysts c USING (catalyst_id) "
        "WHERE c.catalyst_id IS NULL",
    "rel_missing_entity":
        "SELECT rel_id FROM relationships WHERE entity_a_id NOT IN (SELECT entity_id FROM entities) "
        "OR entity_b_id NOT IN (SELECT entity_id FROM entities)",
    "rel_missing_lifecycle_catalyst":
        "SELECT rel_id FROM relationships WHERE "
        "(materialized_by_catalyst_id IS NOT NULL AND materialized_by_catalyst_id NOT IN (SELECT catalyst_id FROM catalysts)) "
        "OR (invalidated_by_catalyst_id IS NOT NULL AND invalidated_by_catalyst_id NOT IN (SELECT catalyst_id FROM catalysts))",
    "alias_missing_entity":
        "SELECT alias FROM entity_aliases WHERE entity_id NOT IN (SELECT entity_id FROM entities)",
    "placeholder_source_url":                  # same rule as db.clean_url
        "SELECT entry_id FROM catalysts WHERE source_url IS NOT NULL "
        "AND NOT regexp_matches(source_url, $url_re)",
}


def check_db(con: duckdb.DuckDBPyConnection) -> dict[str, list]:
    return {name: [r[0] for r in con.execute(sql, {"url_re": URL_SQL_RE} if "$url_re" in sql else None).fetchall()]
            for name, sql in _DB_CHECKS.items()}


def run_integrity_check(cfg: RoboticsConfig, fs_client: Any = None) -> dict[str, Any]:
    """Return {issue_count, upstream_deleted_count, <check>: {count, sample}}.

    `issue_count` is real breakage (dangling refs, stale Firestore docs);
    `upstream_deleted` is informational — those cards are kept.
    """
    con = duckdb.connect(cfg.duckdb_path, read_only=True)
    try:
        found = check_db(con)
        ckg_ids = {r[0] for r in con.execute(
            "SELECT entry_id FROM catalysts WHERE sector = ?", [cfg.sector]).fetchall()}
    finally:
        con.close()

    fs_error = None
    if fs_client is not None:
        try:
            found.update(_check_firestore(cfg, fs_client, ckg_ids))
        except Exception as exc:  # keep the DuckDB results on a Firestore hiccup
            fs_error = f"{type(exc).__name__}: {exc}"

    out: dict[str, Any] = {k: {"count": len(v), "sample": v[:_SAMPLE]} for k, v in found.items() if v}
    out["issue_count"] = (sum(len(v) for k, v in found.items() if k != "upstream_deleted")
                          + (1 if fs_error else 0))
    out["upstream_deleted_count"] = len(found.get("upstream_deleted", []))
    if fs_error:
        out["firestore_error"] = fs_error
    return out


def _check_firestore(cfg: RoboticsConfig, fs_client: Any, ckg_ids: set[str]) -> dict[str, list]:
    """Upstream-deleted findings + CKG card docs out of sync with DuckDB.

    firestore_missing_card assumes the export ships every card
    (card_limit.time_window_days = 0, the prod setting).
    """
    from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: PLC0415

    upstream = {d.id for d in fs_client.collection(cfg.firestore.collection)
                .where(filter=FieldFilter("category", "==", cfg.sector)).select(["__name__"]).stream()}
    found = {"upstream_deleted": sorted(ckg_ids - upstream)}
    if cfg.firestore_export.enabled:
        items = (fs_client.collection(cfg.firestore_export.collection)
                 .document(cfg.firestore_export.catalysts_subpath).collection("items"))
        fs_ids = {d.id for d in items.select(["__name__"]).stream()}
        found["firestore_stale_card"] = sorted(fs_ids - ckg_ids)
        found["firestore_missing_card"] = sorted(ckg_ids - fs_ids)
    return found
