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
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from . import db, direction, events, hashtags, subtypes
from .config import RoboticsConfig
from .extract import ExtractionResult, extract, research_sources_json
from .resolve import Decision, decide_entities, resolve_entity


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
    # (entry, ExtractionResult) pairs whose review flags carry sequence-assigned
    # rel_ids: flagged by the caller only AFTER the DuckDB push succeeds
    # (a rejected push rewinds the ids; see flag_pending_reviews).
    pending_review: list = field(default_factory=list)

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
            d["source_url"] = db.clean_url(d.get("source_url"))   # "None" → None
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


def is_internal_event(con: duckdb.DuckDBPyConnection, rel: dict, entity_id: int, symmetric: set[str]) -> bool:
    """A self-loop worth keeping as an internal event (docs/ticket-internal-events.md): the extractor named
    two DIFFERENT things that resolved to one real entity ("Optimus" → Tesla, "Tesla"), with a directional
    type. Noise otherwise: the same name on both ends, a two-party type, or a generic group bucket."""
    ea = str(rel.get("entity_a") or "").strip().casefold()
    eb = str(rel.get("entity_b") or "").strip().casefold()
    if not ea or ea == eb or rel.get("rel_type") in symmetric:
        return False
    row = con.execute("SELECT type FROM entities WHERE entity_id = ?", [entity_id]).fetchone()
    return bool(row) and row[0] != "group"


def write_extraction(
    con: duckdb.DuckDBPyConnection,
    entry: dict[str, Any],
    result: ExtractionResult,
    cfg: RoboticsConfig,
    decisions: dict[str, Decision] | None = None,
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
        discarded: set[str] = set()   # generic / ungrounded names already discarded on this card (log once)
        context = " ".join(x for x in (result.headline, entry.get("finding") or "",
                                       getattr(result, "context_text", "") or "") if x)
        for e in result.entities:
            resolved_name = e.get("resolved") or e.get("mention") or ""
            if not resolved_name:
                continue
            if resolved_name in name_to_id or resolved_name in discarded:
                continue
            r = resolve_entity(
                con,
                resolved_name=resolved_name,
                mention=e.get("mention") or resolved_name,
                ticker=e.get("ticker"),
                type_=e.get("type") or "organization",
                catalyst_id=catalyst_id,
                cfg=cfg,
                decision=(decisions or {}).get(resolved_name.strip()),
                context=context,
                descriptor=e.get("kind") == "descriptor",   # the extraction's own label (2026-11-10)
            )
            if r.entity_id is not None:   # None: a discarded generic phrase — its edges are skipped below
                name_to_id[resolved_name] = r.entity_id
            else:
                discarded.add(resolved_name)

        # Insert relationships whose endpoints were resolvable.
        symmetric = direction.symmetric_types()
        written: set[tuple[int, str, int]] = set()
        for rel in result.relationships:
            a_id = name_to_id.get(rel.get("entity_a"))
            b_id = name_to_id.get(rel.get("entity_b"))
            if a_id is None or b_id is None:
                log.warning(
                    "skipping relationship with unresolved endpoint in %s: %s",
                    entry.get("entry_id"), rel,
                )
                continue
            status = "active"
            if a_id == b_id:   # both names resolved to one entity
                if not is_internal_event(con, rel, a_id, symmetric):
                    continue
                status = "internal"
            if rel["rel_type"] in symmetric and a_id > b_id:
                a_id, b_id = b_id, a_id          # one stored direction for symmetric types
            if (a_id, rel["rel_type"], b_id) in written:
                continue
            written.add((a_id, rel["rel_type"], b_id))
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
                status=status,
            )

        # Pass-3 "unclear" edges → their rel_ids, for the review queue (flagged
        # by the caller only after the write succeeds).
        unclear = []
        for rel in result.relationships:
            if rel.get("direction_unclear"):
                a_id, b_id = name_to_id.get(rel.get("entity_a")), name_to_id.get(rel.get("entity_b"))
                row = con.execute("""SELECT rel_id FROM relationships WHERE catalyst_id = ? AND entity_a_id = ?
                                     AND rel_type = ? AND entity_b_id = ? ORDER BY rel_id DESC LIMIT 1""",
                                  [catalyst_id, a_id, rel["rel_type"], b_id]).fetchone() if a_id and b_id else None
                if row:
                    unclear.append({"rel_id": row[0], "edge": f"{rel['entity_a']} --{rel['rel_type']}--> {rel['entity_b']}",
                                    "statement": rel.get("direction_statement"),
                                    "path": [f"1. Extracted: {rel['entity_a']} --{rel['rel_type']}--> {rel['entity_b']} "
                                             f"[{rel.get('evidence_type')}, confidence {float(rel.get('confidence') or 0):.2f}]",
                                             f"2. Mechanism: {rel.get('mechanism') or '—'}",
                                             f"3. Direction verifier ({cfg.extraction.model}, thinking "
                                             f"{cfg.extraction.direction_thinking_level}): unclear — "
                                             f"\"{rel.get('direction_statement') or ''}\"",
                                             f"4. Verifier reason: {rel.get('direction_reason') or '—'}",
                                             "5. Kept as extracted (fail-open) → needs a human"]})
        result.enrichment["direction_unclear"] = unclear

        # Tags are derived from the rows just written, so compute them inside
        # the same transaction — a catalyst is never visible without them.
        hashtags.store_for_catalyst(con, catalyst_id)
        events.refresh(con, [catalyst_id])

        # Record the Pass-3 direction check so the audit tool's MISSING mode
        # skips this card. Absent, failed or partial check (a verdict missing)
        # → no row → `make direction-audit MISSING=1` picks it up.
        dc = (result.enrichment or {}).get("direction_check") or {}
        if "checked" in dc and not dc.get("missing"):
            con.execute(
                "INSERT OR REPLACE INTO direction_audits (entry_id, model, checked, flipped, unclear, source) "
                "VALUES (?, ?, ?, ?, ?, 'ingest')",
                [entry.get("entry_id"), result.model, dc["checked"], dc["flipped"], dc["unclear"]])

        con.execute("COMMIT")
        return catalyst_id
    except Exception:
        con.execute("ROLLBACK")
        raise


def _decide_entities(con: duckdb.DuckDBPyConnection, cfg: RoboticsConfig, entry: dict[str, Any],
                     result: ExtractionResult, *, flag: bool = True) -> dict[str, Decision]:
    """LLM tier of entity resolution, outside the write transaction. Fail-safe:
    on error every non-exact name becomes a new entity (never a wrong merge).
    Unsure claims are flagged for an admin (flag=False: caller flags later,
    e.g. only after its DB push succeeded — see flag_unsure)."""
    if not cfg.resolution.llm:
        return {}
    try:
        from google import genai  # noqa: PLC0415

        decisions = decide_entities(con, cfg, genai.Client(api_key=cfg.extraction.api_key),
                                    text=f"{result.headline}. {entry.get('finding') or ''}",
                                    entities=result.entities)
    except Exception as exc:  # noqa: BLE001
        log.warning("entity_decide_failed entry_id=%s err=%s", entry.get("entry_id"), exc)
        return {}
    if flag:
        flag_unsure(cfg, entry, decisions)
    return decisions


def flag_unclear_directions(cfg: RoboticsConfig, entry: dict[str, Any], result: ExtractionResult) -> int:
    """Review queue: edges the Pass-3 direction verifier (high thinking) could
    not settle — kept as extracted, a human decides. One entry per rel_id."""
    items = (result.enrichment or {}).get("direction_unclear") or []
    if not items:
        return 0
    try:
        from .upstream_dedup import flag_attention  # noqa: PLC0415

        fs = _firestore_client(cfg)
        return sum(flag_attention(fs, f"rel{it['rel_id']}", "direction_unclear",
                                  f"Direction verifier couldn't tell which way {it['edge']} goes "
                                  f"(kept as extracted). {it.get('statement') or ''}".strip(),
                                  rel_id=it["rel_id"], entry_id=entry.get("entry_id"), category=cfg.sector,
                                  path=it.get("path") or [])
                   for it in items)
    except Exception as exc:  # noqa: BLE001 — review flags never fail an ingest
        log.warning("flag_unclear_failed entry_id=%s err=%s", entry.get("entry_id"), exc)
        return 0


def flag_pending_reviews(cfg: RoboticsConfig, pending: list) -> int:
    """Call after the DuckDB push succeeded (or when there is no push)."""
    return sum(flag_unclear_directions(cfg, entry, result) for entry, result in pending or [])


def flag_unsure(cfg: RoboticsConfig, entry: dict[str, Any], decisions: dict[str, Decision]) -> None:
    unsure = {n: d for n, d in decisions.items() if d.review}
    if unsure:
        try:
            from .upstream_dedup import flag_attention  # noqa: PLC0415

            fs = _firestore_client(cfg)
            for name, d in unsure.items():
                flag_attention(fs, f"{name}~{entry.get('entry_id')}", "entity_ingest_unsure",
                               f"Ingest kept {name!r} as a new entity: the LLM was unsure "
                               f"({'; '.join(d.review)}, confidence {d.confidence:.2f}). {d.reason} "
                               f"Fix with make entity-merge / entity-link if needed.",
                               entry_id=entry.get("entry_id"), category=cfg.sector,
                               path=d.path + [f"5. Not applied (below the bar) → kept as a new entity: {'; '.join(d.review)}"])
        except Exception as exc:  # noqa: BLE001
            log.warning("entity_review_flag_failed entry_id=%s err=%s", entry.get("entry_id"), exc)


# ── Main orchestrator ──────────────────────────────────────────────


def run_ingest(
    cfg: RoboticsConfig,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> IngestResult:
    pending_review: list = []
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

        # One-time (then no-op) backfill for catalysts written before
        # hashtags existed. Deterministic, local, milliseconds per row.
        tagged = hashtags.backfill(con)
        if tagged:
            log.info("hashtags_backfill tagged=%d", tagged)
        # Relationship subtypes: full recompute, writes only changed rows — the
        # first run backfills, a later rules change self-heals. Milliseconds.
        subtyped = subtypes.refresh(con)
        if subtyped:
            log.info("subtypes_refresh changed=%d", subtyped)
        evented = events.refresh(con)    # card event_type: same pattern (first run = backfill)
        if evented:
            log.info("event_type_refresh changed=%d", evented)

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
        record_lookup = direction.on_record_lookup(con)   # Pass-3 context
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
                result = extract(e, cfg, on_record=record_lookup)
                decisions = _decide_entities(con, cfg, e, result)
                write_extraction(con, e, result, cfg, decisions)
                pending_review.append((e, result))
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
            pending_review=pending_review,
        )
    finally:
        con.close()
