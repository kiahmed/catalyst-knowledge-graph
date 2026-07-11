# Spec: per-entity social handle resolution (for soljet-postiz tagging)

**Owner:** catalyst-knowledge-graph (this repo). **Consumer:** soljet-postiz.
**Status:** IMPLEMENTED 2026-07-07 (both contracts). Drafted 2026-07-05 from
the handle-strategy analysis.

> **2026-07-11 — slug → URN resolution now lives in soljet-postiz.** Division of
> labour is settled: this repo stores only the **verified vanity slug**
> (`@spiritai-robotics`); soljet-postiz turns slug → **organization URN**
> (`urn:li:organization:111476012`) at post time via LinkedIn's authenticated
> `GET /rest/organizations?q=vanityName&vanityName=<slug>` finder, using the
> LinkedIn app token Postiz already holds. LinkedIn only renders a *real* mention
> from the token `@[Name](urn:li:organization:<id>)`; a bare `@slug` is dead text.
> The app-token scope was **verified live** — `rw_organization_admin` resolves
> arbitrary third-party orgs; no `r_organization_lookup` / Community-Management
> product access needed (that caveat, item under "Open decisions", is retired).
> **This repo does NOT need to store the URL or the numeric org id** — both are
> derivable from the slug downstream. The one irreplaceable upstream job remains:
> pick the RIGHT company among same-named orgs (`spirit-ai` the games firm vs
> `spiritai-robotics`), which needs graph/sector context unavailable at post
> time. Postiz-side impl: `src/lib/linkedin_urn.py`, `docs/linkedin-mentions.md`.

> Implementation notes (2026-07-07): resolution is fully deterministic — no
> LLM. Storage deviates from the schema sketch below by design: instead of
> columns on `entities`, handles live in a **shared `entity_handles` table**
> keyed by (name_key, channel) — per canonical entity, never per entry, and
> channel-extensible (reddit later without migration). Core logic:
> `src/handles.py`; service: `tools/handle-resolver/` (compose service on
> `HANDLE_PORT`, DuckDB cache; Cloud Function `handle-resolver` in prod,
> Firestore `handle-cache` collection). Contract A: `make handles` sweep →
> `make export` embeds `linkedin_handle`/`x_handle` on card entities.
> Contract B: POST to the service/function. `source='blocked'` rows mark
> LinkedIn 999 rate-walls and are retried on the next sweep; sweeps
> circuit-break after 3 consecutive blocks. X handles: cache/human only
> (paid API still an open decision).

## Why this lives here

soljet-postiz posts KG cards to X + LinkedIn. To drive traffic it wants to
@-mention the **subject** company of each card with the *correct handle for that
platform* (Figure AI is `@figure-ai` on LinkedIn but `@Figure_robot` on X). The
post body is identical across channels; only the appended tag differs.

Resolution belongs **upstream, here**, because this repo already canonicalizes
and dedupes entities (`src/resolve.py`, `entity_id` + aliases + the
`unresolved_entities` staging queue). Resolving a handle **once per canonical
entity** means it's reused across every card, channel, and post for free.
Doing it in the posting repo would re-resolve flat name strings per post with no
cache and no canonicalization.

soljet-postiz already selects the subject entities **deterministically, no LLM**
(`primary_entities()` ranks by relationship confidence×impact), and already has
the injection seam (`handles.resolve_handle`). It needs this repo to supply
verified handles; it changes **no code** to consume them — only config.

## Core rule: verify-or-abstain

A wrong @ tags the wrong real company (public reputational error), so **precision
over coverage** — abstain rather than guess.

| Entity kind | Handle source | Notes |
|---|---|---|
| Public company (has `ticker`) | `$TICKER` cashtag — deterministic, no discovery | soljet-postiz already emits these from `ticker`; no handle needed unless you want the actual account |
| Any company, **LinkedIn** | web/google search → **WebFetch the `/company/<slug>` page → require name/purpose match** against the entity | abstain on multiple official/regional accounts or common-word names ("Humanoid") |
| Any company, **X** | **NOT fetch-verifiable** — `x.com`/`twitter.com` return HTTP 402 to scrapers | accept ONLY from the paid **X API v2** username lookup, or human-confirmed entries; else abstain |
| Ambiguous / common word | — | abstain (no handle) |

Low-confidence candidates go to a review queue (reuse the `unresolved_entities`
pattern); a human promotes them to `verified`.

## Entity schema additions (`src/db.py` entities table, per-sector DuckDB)

```
linkedin_handle     TEXT     -- e.g. "@figure-ai", or NULL (abstained)
x_handle            TEXT     -- e.g. "@Figure_robot", or NULL
handle_confidence   REAL     -- 0..1
handle_source       TEXT     -- 'linkedin_verified' | 'x_api' | 'human' | 'abstain'
handle_resolved_at  TIMESTAMP
```

Scope is per-sector (each sector has its own DuckDB + `CKG-<sector>` Firestore
collection); accepted duplication. Resolve once per entity that lacks handles,
in `write_extraction()` or a re-runnable post-ingest step, then re-sync to GCS.

## Two delivery contracts (implement either or both)

soljet-postiz consumes via a priority chain (card-embedded → endpoint → local
override), so **either** of these works with no posting-side code change:

### A. Embedded on the card (RECOMMENDED — cached, no runtime call)

`export.py _load_cards` selects the new columns and emits them on **each entity**
in the card payload (both `cards.json` and the Firestore `CKG-<sector>` push):

```json
{
  "card_id": "ROB-051326-003",
  "entities": [
    { "name": "Schaeffler Group", "type": "public_company", "ticker": "SHA",
      "linkedin_handle": "@schaeffler-group", "x_handle": null },
    { "name": "Humanoid", "type": "private_company", "ticker": null,
      "linkedin_handle": null, "x_handle": null }
  ]
}
```

`null` = abstained (posting side simply appends no tag for that entity/channel).

### B. Resolver endpoint (OPTIONAL — for backfilling live without re-ingest)

A Cloud Function; its URL goes in the posting tier.config `HANDLE_ENDPOINT_URL`.

```
POST <url>
  { "entities": ["Figure AI", "Humanoid"], "channels": ["linkedin", "x"] }
→ 200
  { "Figure AI": { "linkedin": "@figure-ai", "x": "@Figure_robot" },
    "Humanoid":  { "linkedin": null, "x": null } }
```

Return only VERIFIED handles; omit/`null` everything else. Keys echo the request
entity names. The posting side caches per (url, entity).

## What soljet-postiz does (already built, no change needed)

- `primary_entities(card)` → subject entities (deterministic).
- `handles.resolve_handle(entity, channel, tier)` → chain: entity's
  `linkedin_handle`/`x_handle` → `HANDLE_ENDPOINT_URL` → local
  `products/_shared/handles.json` override → None.
- `channel_dispatch.entity_tags` builds the per-channel tag list per
  `ENTITY_TAG_MODE` (prefer_handle | handle_only | cashtag_only | both), gated by
  `HANDLE_INJECTION` and `CASHTAGS_ENABLED`, and appends before the deep link.
- **LinkedIn only:** the resolved slug is passed through
  `linkedin_urn.to_mention()` → the vanityName finder → the real mention token
  `@[Official Name](urn:li:organization:<id>)`. **Fail closed**: a wrong/stale
  slug returns nothing and the tag is silently dropped (no dead mention). X keeps
  the plain `@handle`. Results cache in `data/linkedin_urn_cache.json`.
- **So the only field this repo must get right is the slug** (embedded
  `linkedin_handle`, or served by the endpoint). Precision of *which company* is
  what matters — a wrong slug just yields no tag, but a slug pointing at the wrong
  same-named org yields a wrong tag.

To activate once this repo ships: set the card field (A) or `HANDLE_ENDPOINT_URL`
(B), then `HANDLE_INJECTION="true"` in the product's tier.config. No code change.

## Rollout

1. **Now:** posting continues — clean body + hashtags + `$TICKER` cashtags. No @.
2. Add the entity columns + verify-or-abstain resolver (LinkedIn first; X gated
   on the paid API decision).
3. Emit handles on the card (A). Flip `HANDLE_INJECTION=true` per product.
4. (Optional) stand up endpoint (B) for live backfill of old cards.

## Open decisions for the product owner

- **X handles:** pay for X API v2 (only way to auto-verify X), or keep X handles
  human-curated / omitted until then?
- Confirm the **abstain** policy on ambiguous/common-word names (no tag > wrong tag).
- Resolver inline in `write_extraction()` vs a separate re-runnable backfill step.
- ~~LinkedIn org-URN lookup gated behind Community-Management product access —
  verify the app token has it before postiz builds mentions.~~ **Resolved
  2026-07-11:** token scope verified sufficient; postiz-side resolution shipped.
- **Backlog audit:** existing cached handles keep their pre-2026-07-11 values
  (re-resolving costs search credits). Rows with `confidence < 0.9` (the
  "industry unconfirmed — review" class, e.g. the original Spirit AI) should be
  spot-audited before their cards are posted, since a wrong slug → wrong tag:
  `make db-query Q="SELECT * FROM entity_handles WHERE confidence < 0.9 AND handle IS NOT NULL"`.
