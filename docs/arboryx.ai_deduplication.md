# arboryx.ai deduplication — deletion feed, CKG reaction, restores

**From:** arboryx.ai core engine. **Date:** 2026-11-02. **Re:** SR-2026-11 (dedup false positives).

## 1. What changed upstream (one paragraph)

The `"None"`-URL false-positive bug is fixed in both the daily run and the bi-weekly sweep. Placeholder and homepage URLs never URL-match; entity overlap alone never deletes. Borderline pairs are judged by a cheap model (gemini-3.5-flash-lite), asked both ways round. The sweep is now **incremental**: it only considers findings *written* since the last successful sweep (`_synced_at`), triggers daily, does real work every 14 days, and retries the next day if a run is missed or fails. **The first run on the new code (tonight, ~21:00 ET 2026-11-02) is a one-time full scan.** A dry run expects about 6 deletions, including `ROB-070326-005` (UBTech U1, a duplicate of `ROB-070126-002`).

## 2. Deletion feed: where CKG reads what was deleted

Every real (non-dry) sweep run writes one Firestore doc **before** it advances its watermark. The doc is written even if the report email fails.

- **Path:** `dedup_runs/{run_start_iso}` in the same Firestore database as `findings/`. Example id: `2026-11-03T02:00:04.512345+00:00`.
- **Skip** `dedup_runs/state`. That doc is the sweep's watermark, not a run.
- One doc per run. Dry runs and daily triggers skipped as "not due" write nothing. A run that deletes nothing still writes a doc, with `deleted: []`.

```jsonc
{
  "started_at": <timestamp>,            // run start (UTC)
  "result": { "status": "ok", "deleted": 6, "window_since": "...", "llm": {...}, ... },
  "deleted": [                           // one row per deleted finding — flat array
    {
      "category":       "Robotics",
      "target_id":      "ROB-070326-005",   // DELETED from findings/
      "target_ts":      "2026-07-03",
      "matched_id":     "ROB-070126-002",   // KEPT — the surviving original
      "matched_ts":     "2026-07-01",
      "reason":         "url_match" | "tfidf" | "llm_same_event",
      "scores":         { "tfidf": 0.30, "entity": 0.0 },
      "llm_reason":     "..."               // only for llm_same_event
    }
  ]
}
```

The rule is always: **`target_id` is gone, and `matched_id` is the same story and still exists.**

**No finding text or URL in the feed.** Rows carry only IDs, dates, the reason and scores. For the story itself, read `findings/{matched_id}`; CKG's own card for `target_id` also has the deleted text. If you ever need the deleted doc's full content, ask arboryx: point-in-time recovery keeps it for 7 days.

## 3. How CKG should react (recommended, automatic)

Run this after each ingest, next to the existing `integrity_check`:

1. Keep a cursor: the last `dedup_runs` doc id (ISO string, so it sorts) that CKG processed.
2. Query `dedup_runs` for doc ids greater than the cursor, excluding `state`, oldest first.
3. For each row in `deleted` where CKG ingests that `category`:
   - **CKG has a card for `target_id` and one for `matched_id`:** merge. Move any graph edges, mini-graph links, recap mentions and postiz post references from `target_id` to `matched_id`, then retire the `target_id` card.
   - **CKG has a card for `target_id` but none for `matched_id`:** keep the card's content, re-point its upstream reference to `matched_id` (fetch `findings/{matched_id}` for fresh fields), and retire the old id.
   - **CKG has no card for `target_id`:** nothing to do.
   - Key each action by `target_id` so a re-run never acts twice.
4. Advance the cursor only after the whole doc is processed.
5. **Cross-check:** if `integrity_check` reports an `upstream_deleted` id that appears in **no** `dedup_runs` row, it is an unexpected deletion. Alert on it; don't auto-act.

Access: the CKG service account needs read on `dedup_runs` (the same role that already reads `findings/`).

## 4. Option 3 (proposal, not implemented): soft delete / repointer

Instead of deleting the doc, the sweep would keep it and mark it `duplicate_of: "<matched_id>"` plus `deduped_at`, and bump `_synced_at`. The finding becomes a pointer to the surviving record.

- **Not yet ingested:** CKG's normal ingest sees `duplicate_of` and links or skips it instead of making a new card.
- **Already ingested:** depends on how CKG ingests.
  - *If CKG re-reads docs whose `_synced_at` changed:* the bumped timestamp brings the doc back through ingest, and CKG applies the same merge/re-point as in section 3. This needs no extra plumbing.
  - *If CKG ingests each `entry_id` once and never re-reads it:* add a periodic reconciliation that queries `findings` where `duplicate_of != null` and applies the merge/re-point to any card it still holds.
- **Cost on our side:** every reader of `findings/` (the arboryx dedup baseline, the admin API and CKG) must learn to filter or redirect on `duplicate_of`.
- **Benefit:** no data ever disappears, and the redirect lives on the record itself instead of a separate feed.
- We can switch to this if CKG prefers it. Tell us which ingest model applies.

## 5. Restored

**Restored by arboryx (2026-11-02):** 37 findings, recovered from Cloud Logging save records.
- They have their **original `entry_id`s**, with all fields intact (finding, sentiment_takeaways, guidance_play, price_levels, source_url, tooltip).
- Each carries `_restored: "SR-2026-11"`, and `_synced_at` was refreshed.
- One gap: `AIS-102126-002` has `price_levels: null` (that line was lost in logging).
- **None are Robotics.**

`AIS-101126-002`, `AIS-101126-003`, `AIS-101126-004`, `AIS-101126-005`, `AIS-101126-006`, `AIS-101126-007`, `AIS-101126-008`, `AIS-102126-002`, `AIS-102126-003`, `AIS-102126-004`, `AIS-102126-005`, `AIS-102126-006`, `AIS-102126-007`, `AIS-102126-008`, `AIS-102326-001`, `AIS-102326-002`, `AIS-102326-003`, `AIS-102326-004`, `AIS-102326-005`, `AIS-103026-002`, `PWE-100726-002`, `PWE-102026-002`, `PWE-102026-003`, `PWE-102026-004`, `PWE-102026-005`, `PWE-102026-006`, `PWE-102026-007`, `PWE-102826-002`, `PWE-102826-003`, `PWE-102826-004`, `PWE-102826-005`, `SPD-102426-002`, `SPD-102526-001`, `STM-102626-002`, `STM-102626-003`, `STM-102626-004`, `STM-102626-005`

**Robotics, restored by CKG (2026-11-02 ~16:32 UTC):** arboryx re-checked all 18 in `findings/`. Each is present and complete (finding, sentiment_takeaways, guidance_play and tooltip all populated).

`ROB-091126-001`, `ROB-091426-002`, `ROB-091426-003`, `ROB-091426-004`, `ROB-091426-005`, `ROB-092126-002`, `ROB-092126-003`, `ROB-092126-004`, `ROB-092126-005`, `ROB-092126-006`, `ROB-100626-002`, `ROB-100626-003`, `ROB-100626-004`, `ROB-100726-002`, `ROB-101426-002`, `ROB-101426-003`, `ROB-101426-004`, `ROB-102926-002`

**CKG action:** for each of these 18 IDs, confirm that CKG has its card and that the card's relationship references (graph edges, mini-graphs, recap and postiz links) are intact and point at these exact `entry_id`s. Fix any card that is missing or mis-linked.

**Deleted on purpose:** `ROB-082626-002`, the real duplicate of `ROB-083126-006` named in the SR.

**Not restored:**
- `PWE-102526-001` and `SPD-110126-001` are real duplicates (of `PWE-102326-001` and `SPD-103026-001`).
- 60 findings from the 2026-10-02 and 2026-10-16 sweeps only have their text left, with no analysis fields, so they are on hold. None of them are Robotics.

**Heads-up:**
- **Tonight's full scan** re-evaluates everything, including the restored IDs, under the new rules. Anything it deletes appears in `dedup_runs` (section 2).
- **Refreshed `_synced_at`:** restored docs have a new `_synced_at`. If CKG's ingest keys on `_synced_at`, they may show up as updated. They keep their original `entry_id`s, so treat them as updates, not new cards.
