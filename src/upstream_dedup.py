"""React to Arboryx dedup deletions (feed: Firestore `dedup_runs/{run_iso}`).

Contract (arboryx.ai_deduplication.md §2): each run doc lists `deleted`
rows; `target_id` is gone upstream, `matched_id` is the same story and kept.

Per row in our sector, keyed by target_id (never acts twice):
  - we hold target AND matched → remove our target card (rows, relationships,
    Firestore card doc) and record it; the export adds `merged_from` to the
    kept card so old /card/<target> links land on it.
  - we hold target but NOT matched → no action; admin attention entry.
  - we don't hold target → record 'no_card', nothing else.
Card PNGs are left in the bucket (ingest SA has no cards-bucket access; an
old share link still shows its preview, then redirects).

Upstream deletions missing from the feed are flagged by flag_unexpected()
from the integrity check — never auto-acted on.
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb

from .config import RoboticsConfig

log = logging.getLogger("robotics.upstream_dedup")

FEED_COLLECTION = "dedup_runs"
ATTENTION_PATH = ("admin_sessions", "attention_needed", "entries")
SOURCE = "robotics_CKG"


def flag_attention(fs_client: Any, doc_id: str, kind: str, reason: str, **details: Any) -> bool:
    """Create an admin entry (resolved=False). Deterministic id: one entry per
    (doc_id, kind), so daily re-runs don't pile up or reopen a resolved one."""
    from google.api_core.exceptions import AlreadyExists  # noqa: PLC0415
    from google.cloud import firestore  # noqa: PLC0415

    col, parent, sub = ATTENTION_PATH
    ref = fs_client.collection(col).document(parent).collection(sub).document(f"{SOURCE}__{doc_id}__{kind}")
    try:
        ref.create({"source": SOURCE, "created_at": firestore.SERVER_TIMESTAMP, "doc_id": doc_id,
                    "kind": kind, "reason": reason, "resolved": False, **details})
        return True
    except AlreadyExists:
        return False


def _remove_card(con: duckdb.DuckDBPyConnection, target: str, target_cid: int, matched_cid: int,
                 run_id: str, matched: str) -> int:
    """Remove our target card; the catalyst delete and its upstream_merges row
    commit together, so any failure before that leaves the target present and
    the next run redoes it (every step before is idempotent)."""
    # Lifecycle refs to the target move to the kept card (never onto itself).
    for col in ("materialized_by_catalyst_id", "invalidated_by_catalyst_id"):
        con.execute(f"UPDATE relationships SET {col} = CASE WHEN catalyst_id = ? THEN NULL ELSE ? END "
                    f"WHERE {col} = ?", [matched_cid, matched_cid, target_cid])
    n = con.execute("SELECT COUNT(*) FROM relationships WHERE catalyst_id = ?", [target_cid]).fetchone()[0]
    # Separate statements: DuckDB rejects deleting an FK-referenced catalyst in
    # the same transaction as the relationships that reference it.
    con.execute("DELETE FROM relationships WHERE catalyst_id = ?", [target_cid])
    con.execute("DELETE FROM unresolved_entities WHERE catalyst_id = ?", [target_cid])
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM catalysts WHERE catalyst_id = ?", [target_cid])
        con.execute("DELETE FROM export_state WHERE doc_key = ?", [f"catalysts/{target}"])
        con.execute("INSERT INTO upstream_merges (target_id, matched_id, run_id, action) VALUES (?, ?, ?, 'merged')",
                    [target, matched, run_id])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def process_feed(cfg: RoboticsConfig, fs_client: Any) -> dict[str, Any]:
    """Apply every unprocessed dedup run. Returns a summary for logging.

    A failure raises; the run isn't marked done, so the next ingest retries it
    (already-recorded targets are skipped)."""
    con = duckdb.connect(cfg.duckdb_path)
    summary: dict[str, Any] = {"runs": 0, "merged": [], "no_card": 0, "attention": []}
    try:
        from .firestore_export import _STATE_DDL  # noqa: PLC0415

        con.execute(_STATE_DDL)   # first prod run may precede any export
        cursor = con.execute("SELECT COALESCE(MAX(run_id), '') FROM dedup_feed_runs").fetchone()[0]
        run_ids = sorted(d.id for d in fs_client.collection(FEED_COLLECTION).select(["__name__"]).stream()
                         if d.id != "state" and d.id > cursor)
        items = (fs_client.collection(cfg.firestore_export.collection)
                 .document(cfg.firestore_export.catalysts_subpath).collection("items"))
        for run_id in run_ids:
            doc = fs_client.collection(FEED_COLLECTION).document(run_id).get().to_dict() or {}
            rows = [r for r in doc.get("deleted") or [] if r.get("category") == cfg.sector]
            for r in rows:
                target, matched = r.get("target_id"), r.get("matched_id")
                if not target or not matched or con.execute(
                        "SELECT 1 FROM upstream_merges WHERE target_id = ?", [target]).fetchone():
                    continue
                # matched may itself have been merged away already (same batch
                # or earlier run): resolve to the card that survives.
                merged_to = dict(con.execute(
                    "SELECT target_id, matched_id FROM upstream_merges WHERE action = 'merged'").fetchall())
                seen = {target}
                while matched in merged_to and matched not in seen:
                    seen.add(matched)
                    matched = merged_to[matched]
                cid = dict(con.execute("SELECT entry_id, catalyst_id FROM catalysts WHERE entry_id IN (?, ?)",
                                       [target, matched]).fetchall())
                if target in cid and matched in cid:
                    # Firestore first: if it fails nothing local changed yet, so the next run redoes all of it.
                    if cfg.firestore_export.enabled:
                        items.document(target).delete()
                    rels = _remove_card(con, target, cid[target], cid[matched], run_id, matched)
                    summary["merged"].append({"target": target, "kept": matched, "relationships_removed": rels})
                    continue
                if target not in cid:
                    action = "no_card"
                    summary["no_card"] += 1
                else:
                    action = "attention"
                    flag_attention(fs_client, target, "dedup_matched_missing",
                                   f"Arboryx deleted {target} as a duplicate of {matched}, but CKG has no "
                                   f"card for {matched}. Our {target} card was kept; decide whether to remove it.",
                                   matched_id=matched, run_id=run_id, category=cfg.sector,
                                   dedup_reason=r.get("reason"))
                    summary["attention"].append(target)
                con.execute("INSERT INTO upstream_merges (target_id, matched_id, run_id, action) VALUES (?, ?, ?, ?)",
                            [target, matched, run_id, action])
            con.execute("INSERT INTO dedup_feed_runs (run_id) VALUES (?)", [run_id])   # cursor: whole run done
            summary["runs"] += 1
    finally:
        con.close()
    return summary


def merged_from_map(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """kept entry_id → [removed duplicate ids], following chains (A→B, B→C ⇒ C gets A, B)."""
    to = dict(con.execute("SELECT target_id, matched_id FROM upstream_merges WHERE action = 'merged'").fetchall())
    out: dict[str, list[str]] = {}
    for target in to:
        kept, seen = to[target], {target}
        while kept in to and kept not in seen:
            seen.add(kept)
            kept = to[kept]
        out.setdefault(kept, []).append(target)
    return {k: sorted(v) for k, v in out.items()}


def flag_unexpected(fs_client: Any, upstream_deleted: list[str], cfg: RoboticsConfig) -> int:
    """Integrity found CKG cards whose finding vanished upstream. Skip ones the
    feed already explained (e.g. already flagged 'attention'); flag the rest."""
    con = duckdb.connect(cfg.duckdb_path, read_only=True)
    try:
        known = {r[0] for r in con.execute("SELECT target_id FROM upstream_merges").fetchall()}
    finally:
        con.close()
    return sum(flag_attention(fs_client, eid, "upstream_deleted_unexpected",
                              f"Arboryx findings/{eid} no longer exists and no dedup_runs row explains it. "
                              f"CKG kept its card; decide whether it should be removed.", category=cfg.sector)
               for eid in upstream_deleted if eid not in known)
