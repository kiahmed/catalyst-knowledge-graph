"""Ingest pipeline orchestrator.

Public entry points:
    run_ingest(cfg, dry_run=False, limit=None) -> IngestResult
    write_extraction(con, entry, result, cfg)   -> int  (catalyst_id)

Flow per run:
  1. init_schema (idempotent)
  2. read compound watermark from ingestion_meta
  3. Firestore query: collection=findings, where category == sector,
     order_by (timestamp, __name__), start_after watermark
     (using __name__ reuses Arboryx's existing all-ASC composite index)
  4. set-difference filter against already-processed entry_ids (defense-in-depth)
  5. for each new entry: extract() → write_extraction() (one transaction)
     advance the watermark after each successful write, so a mid-batch
     crash leaves the DB and watermark consistent
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import duckdb
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from . import db
from .config import RoboticsConfig
from .extract import ExtractionResult, extract, research_sources_json
from .resolve import resolve_entity


log = logging.getLogger("robotics.ingest")


@dataclass
class IngestResult:
    sector: str
    entries_read: int            # total robotics entries returned by Firestore (post-watermark)
    entries_new: int             # passed set-difference filter
    entries_written: int         # successfully extracted and written
    entries_failed: int
    last_processed_date: str | None
    last_processed_entry_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "sector": self.sector,
            "entries_read": self.entries_read,
            "entries_new": self.entries_new,
            "entries_written": self.entries_written,
            "entries_failed": self.entries_failed,
            "last_processed_date": self.last_processed_date,
            "last_processed_entry_id": self.last_processed_entry_id,
        }


# ── Composite watermark helpers ────────────────────────────────────


def _parse_date(s: Any) -> date | None:
    if not s:
        return None
    if isinstance(s, date):
        return s
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ── Firestore source ───────────────────────────────────────────────
# Arboryx writes findings to `findings/{entry_id}` (single source of truth
# since 2026-05-10). We filter server-side by category, order by
# (timestamp, __name__) ASC, and page with start_after.
#
# Why __name__ instead of `entry_id`?
#   The doc id in `findings/{entry_id}` IS the entry_id, so __name__ ordering
#   yields the same result. Critically, this lets us reuse Arboryx's existing
#   composite index (category ASC, timestamp ASC, __name__ ASC) without
#   creating a duplicate. See dev-utils/firestore_indexes_bootstrap.sh for
#   the spec + bootstrap script if the index ever needs re-creating.


def _firestore_client(cfg: RoboticsConfig) -> firestore.Client:
    """Lazy client; honors FIRESTORE_DATABASE for non-default DBs."""
    fs = cfg.firestore
    kwargs: dict[str, Any] = {"project": fs.project} if fs.project else {}
    if fs.database and fs.database != "(default)":
        kwargs["database"] = fs.database
    return firestore.Client(**kwargs)


def _fetch_findings(
    cfg: RoboticsConfig,
    last_date: date | None,
    last_id: str,
    page_size: int = 500,
) -> list[dict[str, Any]]:
    """Query Arboryx Firestore for new findings in this sector.

    Server-side: category filter + (timestamp, __name__) ordering + start_after
    cursor (when watermark is set). Pages in chunks of ``page_size`` to bound
    memory; the in-memory set-difference filter still runs on top in run_ingest.
    """
    client = _firestore_client(cfg)
    collection_ref = client.collection(cfg.firestore.collection)
    base = (
        collection_ref
        .where(filter=FieldFilter("category", "==", cfg.sector))
        .order_by("timestamp")
        .order_by("__name__")
    )
    if last_date is not None and last_id:
        # __name__ cursor takes a DocumentReference, not a string id.
        base = base.start_after({
            "timestamp": last_date.isoformat(),
            "__name__": collection_ref.document(last_id),
        })

    out: list[dict[str, Any]] = []
    cursor_q = base.limit(page_size)
    while True:
        page = list(cursor_q.stream())
        if not page:
            break
        for doc in page:
            d = doc.to_dict() or {}
            d.pop("_synced_at", None)
            d.pop("_hash", None)
            out.append(d)
        if len(page) < page_size:
            break
        last_doc = page[-1]
        last_d = last_doc.to_dict() or {}
        cursor_q = base.start_after({
            "timestamp": last_d.get("timestamp", ""),
            "__name__": last_doc.reference,
        }).limit(page_size)
    return out


# ── Write one extraction result (one transaction) ──────────────────


def write_extraction(
    con: duckdb.DuckDBPyConnection,
    entry: dict[str, Any],
    result: ExtractionResult,
    cfg: RoboticsConfig,
) -> int:
    """One transaction: catalyst + entities + relationships. Returns catalyst_id."""
    entry_date = _parse_date(entry.get("timestamp"))

    con.execute("BEGIN TRANSACTION")
    try:
        catalyst_id = db.insert_catalyst(
            con,
            entry=entry,
            headline=result.headline,
            sentiment_label=result.sentiment_label,
            prompt_version=result.prompt_version,
            significance_score=result.significance_score,
            research_sources=research_sources_json(result) or None,
        )

        # Resolve all entities first; build a canonical→id map.
        name_to_id: dict[str, int] = {}
        for e in result.entities:
            resolved_name = e.get("resolved") or e.get("mention") or ""
            if not resolved_name:
                continue
            if resolved_name in name_to_id:
                continue
            r = resolve_entity(
                con,
                resolved_name=resolved_name,
                mention=e.get("mention") or resolved_name,
                ticker=e.get("ticker"),
                type_=e.get("type") or "organization",
                catalyst_id=catalyst_id,
                cfg=cfg,
            )
            name_to_id[resolved_name] = r.entity_id

        # Insert relationships whose endpoints were resolvable.
        for rel in result.relationships:
            a_id = name_to_id.get(rel.get("entity_a"))
            b_id = name_to_id.get(rel.get("entity_b"))
            if a_id is None or b_id is None:
                log.warning(
                    "skipping relationship with unresolved endpoint in %s: %s",
                    entry.get("entry_id"), rel,
                )
                continue
            refs = rel.get("source_refs") or []
            source_refs_json = json.dumps(refs) if refs else None
            db.insert_relationship(
                con,
                catalyst_id=catalyst_id,
                entity_a_id=a_id,
                rel_type=rel["rel_type"],
                entity_b_id=b_id,
                confidence=float(rel.get("confidence") or 0.0),
                flagged=bool(rel.get("flagged", False)),
                evidence_type=str(rel.get("evidence_type") or "direct"),
                mechanism=rel.get("mechanism"),
                mechanism_strength=(
                    float(rel["mechanism_strength"])
                    if rel.get("mechanism_strength") is not None else None
                ),
                impact_magnitude=(
                    float(rel["impact_magnitude"])
                    if rel.get("impact_magnitude") is not None else None
                ),
                first_flagged_at=entry_date,
                source_refs=source_refs_json,
            )

        con.execute("COMMIT")
        return catalyst_id
    except Exception:
        con.execute("ROLLBACK")
        raise


# ── Main orchestrator ──────────────────────────────────────────────


def run_ingest(
    cfg: RoboticsConfig,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> IngestResult:
    con = db.connect(cfg.duckdb_path)
    try:
        db.init_schema(con)

        # Pre-flight orphan sweep. Defense-in-depth against partial state
        # from a prior run that aborted between DuckDB WAL checkpoints or
        # was SIGKILL'd. write_extraction is atomic, so in normal operation
        # this is a no-op. Any non-zero counts are logged for visibility.
        orphan_counts = db.cleanup_orphans(con)
        if any(v for v in orphan_counts.values()):
            log.warning("orphan_sweep counts=%s", orphan_counts)
        else:
            log.info("orphan_sweep clean")

        last_date, last_id, _last_gen_unused = db.get_watermark(con, cfg.sector)

        # Server-side filter: category + start_after(watermark) + order_by(ts, id).
        sector_entries = _fetch_findings(cfg, last_date, last_id)

        # Defense-in-depth: drop anything DuckDB already has (e.g., after a
        # crash between Firestore read and watermark advance).
        processed_ids = db.get_processed_entry_ids(con, cfg.sector)
        new_entries = [
            e for e in sector_entries
            if (e.get("entry_id") or "") not in processed_ids
        ]
        # Firestore returned them ordered, but re-sort defensively in case a
        # malformed timestamp slipped in.
        new_entries.sort(key=lambda e: (str(e.get("timestamp") or ""), e.get("entry_id") or ""))
        if limit is not None and limit > 0:
            new_entries = new_entries[:limit]

        log.info(
            "filter_done sector_entries=%d watermark=(%s,%s) processed_known=%d new=%d",
            len(sector_entries), last_date, last_id, len(processed_ids), len(new_entries),
        )

        written = 0
        failed = 0
        failed_ids: list[str] = []
        # Soft deadline: stop starting NEW extractions once the run has used
        # its budget, so export / DuckDB push / Pub/Sub still get to run and
        # the watermark's partial progress survives. Without it a backlog
        # bigger than the request timeout wedges the pipeline permanently:
        # every run is killed mid-extraction, nothing is published, and the
        # backlog grows (observed in prod 2026-08-25/26). 0 = unlimited.
        budget_s = float(os.environ.get("INGEST_TIME_BUDGET_S", "0") or 0)
        run_start = time.monotonic()
        for idx, e in enumerate(new_entries, start=1):
            entry_id = e.get("entry_id") or "?"
            if dry_run:
                continue  # count-only, no LLM call, no writes
            if budget_s and (time.monotonic() - run_start) > budget_s:
                deferred = len(new_entries) - idx + 1
                log.warning(
                    "time_budget_reached budget_s=%.0f written=%d deferred=%d "
                    "next_entry=%s — finishing export/push; rest resumes next run",
                    budget_s, written, deferred, entry_id,
                )
                break
            log.info(
                "extract_start entry_id=%s seq=%d/%d ts=%s",
                entry_id, idx, len(new_entries), e.get("timestamp"),
            )
            t0 = time.monotonic()
            try:
                result = extract(e, cfg)
                write_extraction(con, e, result, cfg)
                # Advance watermark AFTER successful write, so mid-batch crash is safe.
                db.set_watermark(
                    con,
                    cfg.sector,
                    last_date=_parse_date(e.get("timestamp")),
                    last_entry_id=entry_id,
                    last_gcs_gen=None,  # column kept for transition; not used post-Firestore cutover
                )
                written += 1
                last_date = _parse_date(e.get("timestamp"))
                last_id = entry_id
                log.info(
                    "extract_ok entry_id=%s seq=%d/%d duration_s=%.2f edges=%d entities=%d sig=%.2f",
                    entry_id, idx, len(new_entries), time.monotonic() - t0,
                    len(result.relationships), len(result.entities),
                    result.significance_score,
                )
            except Exception as exc:
                failed += 1
                failed_ids.append(entry_id)
                # Structured, single-line failure log so operators can grep
                # `extract_fail entry_id=ROB-...` in stdout to find every
                # failure from a run. Stack trace goes through log.exception.
                log.error(
                    "extract_fail entry_id=%s seq=%d/%d duration_s=%.2f err_type=%s err=%s",
                    entry_id, idx, len(new_entries), time.monotonic() - t0,
                    type(exc).__name__, str(exc)[:500],
                )
                log.exception("extract_fail_traceback entry_id=%s", entry_id)
                # Continue; failed entries stay unprocessed and will be
                # re-attempted on the next run (set-difference filter).

        log.info(
            "run_summary sector=%s written=%d failed=%d "
            "last_processed_entry_id=%s last_processed_date=%s failed_ids=%s",
            cfg.sector, written, failed, last_id,
            last_date.isoformat() if last_date else None,
            failed_ids[:20],  # truncate to avoid flooding stdout
        )

        # Fold the WAL into the main .duckdb file before returning, so the
        # caller can copy a self-consistent single file to GCS (prod). In
        # local mode this is harmless — DuckDB also checkpoints on close.
        if not dry_run:
            con.execute("CHECKPOINT")

        return IngestResult(
            sector=cfg.sector,
            entries_read=len(sector_entries),
            entries_new=len(new_entries),
            entries_written=written,
            entries_failed=failed,
            last_processed_date=last_date.isoformat() if last_date else None,
            last_processed_entry_id=last_id,
        )
    finally:
        con.close()
