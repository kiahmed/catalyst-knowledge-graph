# Ticket: ingest-time entity resolution — stop mislabeling at the source

**Owner:** catalyst-knowledge-graph. **Date:** 2026-11-09. **Status:** built 2026-11-09 (§3.1–3.6), pending review;
migration (§5) not built yet. Defaults taken for §8: (1) generic → tag to a curated `group` bucket or discard (no new entities; see §3.2); (2) generic/place word lists cover the names in today's data; (3) legacy aliases
are unverified unless a safe rule passes (format variant, ticker, initials). Rollout (§6) uses
`make reextract-prod IDS=…` dry runs on known-bad cards instead of 3 log-only days (ingest paused).
**Severity:** high. It writes wrong entity endpoints into prod every day.
**Evidence:** Traction register DA-006/008/009/010/012/013 (2026-11-06..07);
SEER Robotics → Serve Robotics; "Amazon" → AWS; China, Humanoid, "Venture Capital",
"Customers" absorbing descriptor phrases (workbench 2026-11-03, 2026-11-09).

## 1. How ingest resolves names today (`src/resolve.py`, `src/ingest.py`)

Extraction (Pass 1, `src/extract.py`) emits each entity as `mention` (the text as
written) + `resolved` (the LLM's canonical form). For each name:

1. **Tier 1, exact:** `db.find_entity_exact` matches `resolved` or `mention` against
   entity names AND aliases, case-insensitively. A hit is trusted and the LLM never sees it.
   When `resolved` matched and `mention` differs, **the raw mention is saved as a new alias.**
2. **Tier 2, LLM** (`decide_entities`, one call per card): names with no exact
   hit plus fuzzy candidates. The answer is `same_as` (→ reuse, both spellings saved as
   aliases), or new with an optional `parent` / `sister_of` (→ `entity_links`), gated
   at `resolution.min_confidence` 0.8.
3. **Tier 3, new:** create the entity, log it in `unresolved_entities`.

## 2. Root causes

| # | Cause | Example |
|---|---|---|
| RC1 | Tier 1 saves the raw mention as an alias with no check. Extraction often rewrites a descriptor or another org to a known entity, and that rewrite becomes a permanent rule. | "China's leading logistics company" → `resolved` "China" → alias of China |
| RC2 | Tier-1 alias hits are trusted forever, including legacy fuzzy-era aliases. | "SEER Robotics" alias on Serve Robotics; "Samsung SDS" on Samsung SDI |
| RC3 | Generic descriptors are extracted as entities (tier 3) or folded into a real one (RC1). Spelling variants of the same descriptor become separate entities or aliases. | "Customers", "Investors", "leading logistics companies in China" vs "Chinese leading logistic company" |
| RC4 | The alias store keeps case/format variants as separate rows (matching already ignores case). | "Humanoid robots" + "humanoid robots" |
| RC5 | Symmetric types can be stored in both directions. The extraction prompt says to alphabetize, but nothing enforces it, and `direction.dedupe_rows` keys on ordered (a, rel, b). | Intel/NVIDIA competes_with both ways (ROB-082226-003) |

## 3. Design

### 3.1 Name keys (new `src/names.py`, deterministic, free)
- `strict_key(s)`: NFKC → lowercase → strip punctuation → collapse spaces → drop legal
  suffixes (inc, corp, corporation, co, ltd, limited, plc, llc, gmbh, ag, sa, holdings).
  Keeps word order. Answers "is this the same written name?"
- `loose_key(s)`: strict_key → singularize simple plurals → drop filler words
  (the, of, in, a, an, and, for, its, 's) → demonym → place (small table:
  Chinese→china, Japanese→japan, Korean/South Korean→south korea, American/U.S./US→united
  states, European→europe, German→germany, …) → sort tokens.
  "leading logistics companies in China" and "Chinese leading logistic company" both become
  `china company leading logistic`.
- **Safety rule:** `loose_key` must never auto-merge two *named* organizations
  ("Bank of China" vs "China Bank" collide). It is used only (a) for generic phrases
  (3.2), and (b) to add candidates to the LLM call.

### 3.2 Generic-descriptor gate (after the exact canonical / trusted-alias lookup of the mention)
- **As built:** word lists alone can't tell "China Telecom" from "Chinese telecom company", so the *original
  casing* decides. Every word must be generic or a place, AND one generic word is lowercase or plural/collective
  (sector, industry, market…). Title-Case singular names stay names; government phrases go to normal lookup.
  Checked against every entity name in prod (2026-11-09): 74 flagged, all real descriptors.
- A name is **generic** when, after `loose_key`, every token is in a generic
  vocabulary (company, firm, startup, maker, manufacturer, provider, supplier, customer,
  investor, operator, sector, industry, market, leading, major, top, global, domestic,
  logistics, shipping, telecom, automotive, robotics, humanoid, robot, ai, …) or is a place
  token. No proper-noun token remains.
- Generic names never become aliases of a real entity, and are never sent as `same_as` candidates.
- **Product decision (2026-11-09, supersedes the group-entity default below):** a generic phrase is NOT an entity.
  It is tagged only to a descriptor bucket an admin curated: a type `group` entity, one of its aliases, or a
  generic-named legacy entity with the same `loose_key`. Otherwise it is **discarded**: no entity is created, its
  edges are dropped, and it is logged in `unresolved_entities` with `suggested_type='generic_discarded'` for review.
  It is never matched to a real company: lowercase "humanoid" ≠ Humanoid.
- **Superseded (not built), kept for the record — auto-created group entities:** each distinct generic
  `loose_key` maps to ONE group entity (type `group`), whose display name is the first
  phrasing seen. When a place token is present, it is auto-linked `subsidiary_of` that
  place's government entity. So "leading logistics companies in China" and "Chinese leading
  logistic company" land on the same group, a child of China.
  - Alternative: drop edges with a generic endpoint, logged to `unresolved_entities`
    with reason `generic`. **Decision needed.**
- Government phrases ("Chinese government", "Government of China", "Chinese state",
  "Beijing" as government) map deterministically to the country's government entity.
  That is the one generic-looking class allowed as an alias.

### 3.3 Matching order (per extracted name, after the gate)
1. **Canonical name**, exact or `strict_key`, vs `entities.name` → trusted (tier 1).
2. **Verified alias**, exact or `strict_key` → trusted (tier 1).
3. **Unverified alias hit** → NOT trusted. It goes to the existing per-card LLM call as a
   name with that entity (plus family) as candidates. same_as ≥ 0.8 → use it and mark
   the alias verified. Otherwise new or child per the LLM, and the alias is flagged for
   admin review (not deleted automatically). As built: the contradiction is appended to
   `Decision.review`, so `flag_unsure` writes an admin attention entry.
4. Group-entity `loose_key` match (generic names only).
5. Everything else → the existing tier-2 LLM. Candidates = today's fuzzy list +
   `loose_key` hits.

### 3.4 Alias writes (fixes RC1, RC4)
- Save a mention as an alias ONLY when the LLM said same_as ≥ 0.8 for it on this card
  (as built: tier-1 matches write no alias; format variants already match by `strict_key`).
  An extraction rewrite (`resolved` ≠ `mention`) is not evidence by itself. In that case
  the mention goes through 3.3 step 3/5 like any other name.
- `insert_alias` skips a new row whose `case_key` (case/punctuation/spacing only) equals an
  existing alias or the name on the same entity. Legal-suffix variants ("Schaeffler" on
  Schaeffler AG) are still stored, because consumers match the literal alias strings.
- New columns on `entity_aliases`: `verified BOOLEAN`, `source VARCHAR` (ingest_llm |
  ingest_new | generic | admin; NULL = legacy). No `created_at`. Admin alias writes,
  reverts included, are verified/admin.

### 3.5 Children / parents (the "path back" rule)
- Keep the LLM's `parent` / `sister_of` output (already creates the new entity + link).
- Prompt addition: state-owned enterprises, agencies and subsidiaries are NEW entities with
  `parent` = the owner (e.g. China Telecom → parent China). They are never same_as.
  As built this is a prompt rule only, with no code guard (same pattern as the rest of tier 2).

### 3.6 Symmetric edges (fixes RC5)
- At write time, order symmetric endpoints (lower entity_id first). `dedupe_rows`
  keys symmetric types order-free, per card only. Directional types and cross-card
  edges are untouched.

## 4. Cost
No new LLM calls: unverified-alias names and rewritten mentions ride the existing
one-call-per-card `decide_entities` request (slightly longer prompts). Name keys and the
generic gate are pure Python.

## 5. Migration (one-time, report → review → apply, same as entity-apply)
1. Add the alias columns. Legacy aliases start `verified=false, source=legacy`, except those
   passing safe rules (strict_key equal to the name, ticker, initials of the name) → verified.
2. Report the case/format-duplicate aliases → delete the redundant rows.
3. Report existing entities whose name is generic → group by `loose_key` → propose
   merges into one group entity per key + a place-parent link. Admin approves.
4. Symmetric reverse pairs → reject the duplicate (2 live today).

## 6. Rollout
- `resolution.shadow: true` for 3 daily ingests: compute the new decisions, log them
  (`resolution_shadow` events: would-alias, would-group, unverified-alias-sent, generic),
  and change nothing. Compare against what was written.
- Then switch on. Per-ingest counters: tier1_name, tier1_alias_verified, alias_checked,
  llm_same_as, llm_new, llm_child, generic_grouped, alias_writes_skipped.

## 7. Tests (unit, no LLM)
- strict/loose keys: case, punctuation, suffixes, plurals, demonyms, order. "Bank of China"
  ≠ "China Bank" under the merge rules.
- Generic gate: the two logistics phrasings → same group; "China Telecom" not generic.
- Unverified alias → routed to the LLM list (fake client), not trusted.
- Alias write refused for a rewritten mention without same_as; allowed for a format variant.
- insert_alias skips case variants.
- Symmetric write order + order-free dedupe; directional pairs kept.

## 8. Open decisions (product owner)
1. ~~Generic descriptors: group entities vs drop the edge.~~ **Decided 2026-11-09:** tag to a curated `group`
   bucket if one matches, else discard (see §3.2).
2. Demonym/place table scope: start with the countries/regions present in the data.
3. Legacy alias trust: unverified until checked (recommended) vs verified unless flagged.
