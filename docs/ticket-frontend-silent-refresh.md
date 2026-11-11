# Ticket — Web app silent refresh (no browser reload needed)

Status: built 2026-11-11 · frontend + export

## Why
robotics.arboryx.ai is a research tool: a page stays open for hours while ingest runs and admin edits
land. Until now new data showed only after a hard reload (e.g. a new internal event on NVIDIA).

## Requirements
- R1 Every lookup — view switch (Cards / Graph / Recap), any card filter or search, any graph filter / time
  window / company search, opening a node panel or a card — calls `maybeSync()`.
- R2 `maybeSync` is cheap and silent: at most one check per 15 s, one in flight, failures only logged.
  A check reads ONE doc (`CKG-<sector>/catalysts`). If its `version` is unchanged, nothing else is read.
- R3 When `version` moved: fetch only card docs with `synced_at >=` the newest one the page holds (">=" so
  a page that loaded mid-write still gets the rest of that write), drop ids in `deleted_ids` added since,
  merge by `card_id`, rebuild the graph from the cards, re-render the current view; filters, URL hash and
  an open node panel stay as they are. No reload, no spinner.
- R4 Backend (`firestore_export._write_catalysts`): each written card doc gets `synced_at` (one stamp per
  write call; NOT part of the change hash); the parent doc gets `version` = that stamp only when cards were
  written. An admin refresh (`refresh_cards`, partial) no longer overwrites the parent's `count` with the
  few refreshed cards (latent bug fixed).
- R5 Deletes (Arboryx dedup, `upstream_dedup`): append the id to the parent's `deleted_ids` (ArrayUnion) and
  move `version`; best-effort — a failure only means a stale page keeps the card until reload.
- R6 Not refreshed silently: the graph doc's stats / insights / recap (they change once a day with ingest).
  Firestore rules unchanged (signed-in users already read everything under `CKG-*`).

## Review follow-up (CI review, 2026-11-11)
- Silent refresh runs only after the signed-in Firestore load seeded it (`SYNC.ready`) — never on the
  pre-sign-in teaser, where Firebase may not be loaded yet and reads are denied. `checkUpdates` rejects
  (never throws) without the SDK, and the call is wrapped so the busy flag always clears.
- A refresh keeps the user's Cards page (clamped if the list shrank) and re-renders the graph only when
  the Graph tab is on screen.
- Delta window overlaps 30 min before the newest stamp held: stamps are taken before a write commits, so
  an ingest export still writing when an admin save was synced is still picked up.
- Tombstones are "<entry_id>@<stamp>" (always unique → always appended). `deleted_ids` grows by one per
  upstream-dedup delete (a few per month); trim if it ever matters.

## Tests
tests/test_firestore_delta.py `test_silent_refresh_markers`; tests/test_upstream_dedup.py (tombstone).
Headless Chromium with a stubbed `checkUpdates`: a view switch merged a changed card (new headline, NVIDIA
internal event), dropped a deleted one, advanced `version`; a second lookup within 15 s made no call.
