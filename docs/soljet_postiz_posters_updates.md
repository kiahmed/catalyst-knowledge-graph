# Poster/imagery updates for soljet-postiz

**Owner:** catalyst-knowledge-graph (this repo, produces card + graph PNGs).
**Consumer:** soljet-postiz (and any other repo that downloads these images
to post). **Status:** living doc, newest entry at top.

## Why this exists

Every render/imagery-affecting change on this side — new image fields, path/
prefix changes, a bug in how an image gets produced or served — is a silent
breaking change for any consumer that caches or re-derives from these images.
soljet-postiz found one the hard way (2026-10-19, see below) by reading this
repo's code after the fact. Instead of that: whenever a change here could
affect a downstream image consumer, add a dated entry at the top of this
file describing what changed and what the consumer needs to do about it.
Consumers should diff against the last entry they've already acted on.

This is a one-way notice board, not a sync protocol — nothing here runs
automatically. A consumer has to actually read it (or be pointed at it).

---

## [2026-10-27] SHIPPED — 465 edgeless graph posters re-rendered (cards ROB-03xx → mid-Aug)

**What changed here:** every `graphs/<card_id>.png` last written in 2026-09
(cards from March to ROB-081826, i.e. outside the 10-19 backfill) was drawn
without edges and has been overwritten in place with the correct render.
Example: `ROB-031426-006` (posted to LinkedIn 10-26 with 0 edges) now shows 5.

**Consumer action:** none if graph images are fetched pinned to the GCS
object generation (per soljet-postiz, they now are). No container restart
needed. Already-published posts keep the old image. If any cache is still
keyed by URL alone, purge `graph_og_*` entries for these cards.

---

## [2026-10-26b] BUILT, NOT YET DEPLOYED — weekly recap chart cards (§2.9b)

**Heads-up, no action until a "shipped" entry follows.** Two things change once deployed:
1. **Entity insight headlines change wording** (correctness fix): mixed-type
   counts now read `"HD Hyundai: 16 new connections in 30 days (9.6x prior
   90-day rate), led by 9 partnerships"` instead of labelling all 16 as
   partnerships. Single-type entities read as before. New fields on chokepoint
   insights: `top_rel`, `top_rel_count`. If the composer parses the headline,
   switch to these fields.
2. **`recap` block** on `CKG-<sector>/graph/sectors/<sector>`:
   `{week, as_of, window_days, baseline_days, groups, cards:[{id, kind, entity|rel_type,
   insight_headline, title, verdict, big, sub, sentence, data}]}`. `verdict` is the
   ≤10-word takeaway — use it as the post's opening line next to the image.
3. **Hashtags are generated here — don't guess them.**
   - every `graph_insights[]` row gets `hashtags` (entity, its topic, sector;
     sector trends: topic + sector). The digest should union the `hashtags` of
     exactly the rows it posts (after your 21-day skip), in row order, deduped;
   - `recap.hashtags` = a ready-made digest default (named entities + trend
     topic + sector);
   - every recap card has its own `hashtags` for when its image is posted.
   All are priority-ordered and capped at 6 — take a prefix to fit the channel.
4. **Matching a card to what you post:** cards carry `entity` (mix/grounding)
   or `rel_type` (concentration); pick the card whose `entity` is the row you
   chose, image = `recaps/<sector>/<week>/<card.id>.png`. Images at
   `gs://robotics-cards/recaps/<sector>/<week>/<card.id>.png` — week-keyed,
   write-once, safe to cache. Post `cards[0]` (one image per post); the rest
   are the full set.

---

## [2026-10-26] PROPOSAL (no action required yet) — weekly recap gets comparison charts + a canonical page

**What changed here:** nothing built. Analysis only: `technical_spec.md` §2.9b.

**Context for the consumer:** soljet-postiz now posts the Monday sector digest
(`graph_insights[]`, LinkedIn + X) as **text only** — no image and the link is
the bare site root, because there is no single card to point at. §2.9b proposes
what would replace that if built here: three comparison charts (relationship-
mix shift per named entity, dependency/supplier concentration, evidence
grounding) and a canonical `/recap/<YYYY-Www>` page.

**If/when it ships, the consumer-facing contract will be:**
- a `recap` block on `CKG-<sector>/graph/sectors/<sector>`, keyed to the same
  week and entities as the picked insights;
- images at `gs://<bucket>/recaps/<sector>/<YYYY-Www>/<kind>.png` — **week-keyed
  and write-once** (safe to cache on the URL; unlike `graphs/<card_id>.png`);
- a deep link `/recap/<YYYY-Www>` for the post.

**Consumer action today:** none. The digest keeps working text-only. Do not
build against §2.9b until an entry here says it has shipped.

---

## [2026-10-19] Graph poster images can be served stale by caching consumers — no cache-buster on the graph PNG URL

**What changed here:** nothing new — this documents an existing contract gap
just discovered via a real incident (`ROB-101726-005` posted to social with
an old, edge-less graph render while the live site/direct link showed the
correct one).

**Root cause (confirmed by reading soljet-postiz's own code, not a guess):**
`tools/robotics-render/main.py` serves graph posters at a **stable path per
card_id** — `gs://<bucket>/graphs/<card_id>.png` — and re-renders/overwrites
that same object in place (`overwrite=True`) whenever a graph gets
re-rendered (bug fixes, backfills, edge-data corrections, etc.). There is
**no generation marker, no `?g=` cache-buster, no ETag** on the URL itself.

soljet-postiz's `src/lib/imagery.py::_og_graph_image` downloads that URL and
caches the bytes locally keyed by `hash(url)` — and since the URL never
changes, **it never re-downloads**, even after this repo overwrites the GCS
object with a corrected image. Compare `_og_card_image` in the same file,
which deliberately reads the deep-link page's live `og:image` tag (which
*does* carry a `?g=<generation>` buster) specifically so "a regenerated card
re-downloads automatically" — the graph path never got the same treatment.

**Required fix (soljet-postiz side, not done yet):** give `_og_graph_image`
the same cache-busting the card path already has. Concretely: key the local
cache (and ideally the fetch) off something that changes when the GCS object
changes — the blob's `generation` number or `updated` timestamp (one
`bucket.get_blob()` call away, same libs already in use via
`google-cloud-storage`), not a hash of the static URL. Until that lands,
**any** re-render of **any** graph poster can be served stale to an unknown
number of future posts, not just this one card.

**Immediate mitigation (no code change, but temporary and manual):**
Restarting/retriggering the poster container is **not sufficient on its
own** — check first whether `data/imagery_cache/` is a bind mount to host
disk (self-hosted cron path, `docker-compose.yaml`: `volumes: - .:/app`) or
genuinely ephemeral container storage (GCP Cloud Run path). On the bind-
mount path the cache survives restarts and must be cleared by deleting the
stale `graph_og_*.png` files directly; only a fresh Cloud Run revision
(ephemeral local disk) clears it by restarting alone, and only until that
instance gets reused long enough to re-accumulate stale entries the same
way. Treat a restart as "might help today," not as the fix.

**Affected card(s) confirmed so far:** `ROB-101726-005`. Any other card
whose graph was re-rendered after soljet-postiz had already cached its
first graph image is suspect — there's no way to tell from this side which
those are; soljet-postiz would need to cross-check its cache file mtimes
against this repo's GCS object generations.
