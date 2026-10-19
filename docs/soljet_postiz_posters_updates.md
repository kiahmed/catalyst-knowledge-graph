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
