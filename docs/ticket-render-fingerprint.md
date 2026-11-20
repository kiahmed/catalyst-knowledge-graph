# Ticket: render fingerprint — know exactly which card images are out of date

**Status:** built 2026-11-19 (awaiting review + deploy).

## Problem
`make render-stale` compared "Firestore doc changed" with "image rendered". Any export that rewrites docs (adding a
field, a full sync) flags every card — 893 of 948 on 2026-11-19, almost all false. There was no way to tie a data
change (merge, rename, edge added or dropped, evidence change) to the exact images it affects.

## Design
- `src/render_fp.py`: `fingerprint(card)` = `v<FP_VERSION>-<16 hex>`, a sha256 of exactly what the images draw:
  headline, date, subtitle, story flag, every live between-entity edge (from, to, rel, confidence to 2 dp, evidence,
  status, in_story) and every entity (name, ticker, type, in_story). The order doesn't matter. It ignores fields
  the images never draw: hashtags, event_type, subtypes, mechanism, internal events, share text.
- Export writes it into every card doc as `render_fp`.
- The render service stamps the doc's `render_fp` onto each uploaded image's metadata: `cards/<id>.png` and
  `graphs/<id>.png`. It does this on a fresh upload and on the "unchanged bytes" metadata patch.
- `render-stale` lists a card when an image is missing, unstamped ("not fingerprinted"), or stamped with a different
  hash ("content changed"). Docs exported before this change fall back to the old time check.
- When a card or poster TEMPLATE changes what's drawn, bump `FP_VERSION`: every card then re-renders once, on purpose.

## Rollout
Deploy the backend (export) and the render service. The next export writes `render_fp` to every doc, a one-time
full doc rewrite. Then run one `make render-stale APPLY=1`, which renders and stamps every image once. From then on
it lists only cards whose drawing really changed.

## Known limit
The card PNG is drawn from DuckDB and the poster from the Firestore doc. Both come from the same export, so they
agree in normal operation. The stamp is the doc's hash at render time.
