# Proposed Arboryx changes — master log hardening

> **Audience:** Arboryx project context. Implement + test locally in that repo.
> **Why:** the Robotics module (sibling project) needs stable, unique, date-normalized entries in `market_findings_log.json` to support incremental ingestion and entity resolution without re-hashing the whole file. Fixing at the write site keeps every downstream consumer clean.

---

## Scope

Four edits:

1. `market_team.py` — `append_to_memory_log`: add `source_url` param, normalize timestamp, generate unique `entry_id`, reject range-shaped timestamps
2. `market_team.py` — strategist prompt: instruct scout/strategist to pass a source URL per distinct topic call
3. `market_team.py` — `merge_sector_shards`: verify sort correctness now that dates are ISO at write time (likely no code change needed; just confirm)
4. `values.yaml` — `data_engineer_instructions`: add `<output_constraints>` block that locks sentiment vocabulary and takeaway structure (new — see §"Data engineer output constraints" below)

**Not in scope:** migrating the master log itself (the Robotics module owns a one-time corrector utility for existing data). This change is *forward-only* — all new writes must conform.

---

## Final entry schema (8 fields)

```json
{
  "entry_id":            "ROB-041526-001",    // NEW — unique, deterministic
  "timestamp":           "2026-04-15",        // NORMALIZED — always YYYY-MM-DD
  "category":            "Robotics",
  "finding":             "...",
  "sentiment_takeaways": "...",               // RENAMED from insights_sentiment
  "guidance_play":       "...",
  "price_levels":        "...",
  "source_url":          "https://..."        // NEW — optional, null if unavailable
}
```

Field order is not enforced by JSON but keep `entry_id` first for readability.

---

## Unique ID specification

### Format

`XXX-MMDDYY-YYY`

- `XXX` — 3-letter category abbreviation (uppercase)
- `MMDDYY` — entry date (2-digit month, day, year)
- `YYY` — zero-padded counter, resets per `(category, date)` pair

**Example:** `ROB-041526-003` = 3rd Robotics entry dated Apr 15, 2026.

### Category abbreviations (locked)

| Category | Abbreviation |
|---|---|
| Robotics | `ROB` |
| Crypto | `CRY` |
| AI Stack | `AIS` |
| Space & Defense | `SPD` |
| Power & Energy | `PWE` |
| Strategic Minerals | `STM` |

If a new category is added, define its abbreviation in the same 3-letter uppercase style and document it in this file before writing any entries with it.

### Counter derivation — stateless, from the log itself

**Rule:** do not maintain a counter variable or counter file. Derive the next `YYY` by reading the existing log on every write.

```python
def next_entry_id(log: list[dict], category: str, date_iso: str) -> str:
    cat_abbr = CATEGORY_ABBR[category]           # e.g. "Robotics" -> "ROB"
    mm, dd, yy = date_iso[5:7], date_iso[8:10], date_iso[2:4]
    date_part = f"{mm}{dd}{yy}"                   # "041526"

    # Count existing entries with matching category + date
    existing = [
        e for e in log
        if e.get("category") == category and e.get("timestamp") == date_iso
    ]
    yyy = f"{len(existing) + 1:03d}"
    return f"{cat_abbr}-{date_part}-{yyy}"
```

**Why stateless:**
- No counter file to lose, corrupt, or desync on a crash
- ~~Two concurrent runs writing the same `(category, date)` would conflict on *either* approach — solve it with a write lock around `append_to_memory_log` if that's actually possible; otherwise Arboryx is single-writer already, so the log-scan is sufficient~~
- The log is the single source of truth — if the log is right, the IDs are right
- Corrector utility in the Robotics module uses the **same algorithm** on existing entries, guaranteeing coherence across backfill + forward writes

### [Proposed and enhanced by arboryx.ai] — Arboryx is NOT single-writer in practice

The proposal's single-writer assumption is wrong for two reasons, both hit during implementation:

1. **Strategist parallel tool calls.** The strategist prompt (values.yaml:128) tells the LLM to "call `append_to_memory_log` separately" for each topic. Gemini 3.1 honors "separately" as "one call per topic" but is free to emit them as a parallel function-call batch in a single response. Within one sector, two-to-six concurrent appends on the same shard is the common case, not a rare one.
2. **Cross-invocation overlap.** Manual scheduler trigger during a scheduled run, or any operator error that fires the Cloud Function twice, produces two parallel sweeps each running all six sectors. Both see the same master-log state, both compute `-001` for the same `(category, date)`.

Either produces the same race: two callers of `append_to_memory_log` both read the shard at generation N, both compute the same `entry_id`, both append locally, both `blob.upload_from_string(...)`. GCS has no atomic-append primitive — uploads replace the whole object. The later upload **destroys** the earlier one's entry. Silent data loss with no visible duplicate-ID collision for a merge-time fixup to detect.

**Fix applied in Arboryx (this PR):**

1. **Cross-invocation lockout** — `deploy_cloud_func_pipeline.sh` now sets `--max-instances=1 --concurrency=1` on the Cloud Function. Second caller gets HTTP 429 instead of launching a parallel sweep. Closes risk #2 at the infra layer.
2. **Strategist intra-sector lockout** — `append_to_memory_log` now uses GCS `if_generation_match` on every shard upload:
   - `blob.reload()` → capture generation before read
   - Read shard, recompute `entry_id` inside the retry loop (counter must be fresh per attempt)
   - `blob.upload_from_string(..., if_generation_match=gen)` — GCS returns HTTP 412 `PreconditionFailed` if the shard moved under us
   - On 412, loop back, up to 5 attempts. Local mode uses POSIX `flock` for the same serialization guarantee.
3. **Verification** — `arboryx.ai/dev-utils/test_append_race.py` simulates 5 parallel writers through a `threading.Barrier` + a `MockBlob` that enforces real GCS generation semantics. Confirmed 4/5 first-attempt writes triggered `PreconditionFailed`, all 4 losers retried successfully, all 5 entries landed with sequential unique IDs `ROB-…-001`..`-005`, zero data loss.

**Recommendation for the Robotics-module corrector utility:** the same race exists if the module ever runs the corrector concurrently with an Arboryx sweep. Either (a) run the corrector only while the sweep is locked out (use the same `max-instances=1` on any Robotics-module ingestion/correction job that touches the master log), or (b) apply the same `if_generation_match` retry pattern to the module's master-log writes.

### Ordering for multi-entry dates

When multiple entries share `(category, date)`, their `YYY` values are assigned in the order `append_to_memory_log` is called. No semantic meaning attached to the counter beyond "insertion order within that bucket." With the concurrency fix above, counter ordering follows first-winner-of-the-generation-race ordering when the strategist parallelizes — still monotone, still unique, but not necessarily wall-clock-ordered across a parallel batch.

---

## Timestamp normalization

### Rule

Every entry's `timestamp` field must be `YYYY-MM-DD` on write. No exceptions, no time component, no alternate formats.

### Input handling in `append_to_memory_log`

The function should accept a date in any of these forms and normalize before storing:

- Already normalized: `"2026-04-15"` → pass through
- ISO with time: `"2026-04-15 14:23:00"` or `"2026-04-15T14:23:00Z"` → strip to `"2026-04-15"`
- Human-readable: `"March 24, 2026"`, `"Mar 24, 2026"`, `"24 March 2026"` → parse and reformat to `"2026-04-15"`
- Missing/None → raise `ValueError("timestamp required")`. Don't default to "now" — silent date inference is how the original inconsistency happened.

**Reject range-shaped timestamps.** The scout prompt at `values.yaml:72` already instructs `YYYY-MM-DD` format, so ranges like `"March 21-22, 2026"` should never appear on the write path. If one does, raise — it signals a prompt compliance failure that needs fixing upstream, not silent collapse. Detection: any timestamp containing `–`, `—`, ` to `, ` - `, or a digit-hyphen-digit pattern (outside a strict ISO date) → raise.

Recommend `dateutil.parser.parse(ts).strftime("%Y-%m-%d")` as the single normalization path, wrapped behind the range-rejection check. Raise a clear error on unparseable input.

> Note: the Robotics module's one-time corrector tolerates range timestamps and collapses them to the start date (backfill-only affordance for 36 legacy entries). Arboryx going forward must not.

---

## `source_url` field

### Signature change

```python
def append_to_memory_log(
    category: str,
    finding: str,
    timestamp: str,
    sentiment_takeaways: str,        # RENAMED from insights_sentiment
    guidance_play: str,
    price_levels: str,
    source_url: str | None = None,   # NEW
) -> str:                             # NEW — returns the generated entry_id
    ...
```

### Semantics

- `source_url` is the primary source URL for the finding (news article, press release, filing, etc.)
- `None` / missing is allowed — not all findings have a clean single source
- Not displayed to end users by the Robotics module in Phase 1 (per workbench decision). Stored for potential future fact-check automation
- No validation beyond "string or None" — don't HEAD the URL or check reachability; that's not this function's job

### Strategist prompt change

~~Update the prompt so the strategist includes a `source_url` argument when invoking the tool, one per distinct topic call. Keep it optional in the schema — pass-through `None` when the strategist doesn't have one. Do not block a finding from being logged over a missing URL.~~

### [Proposed and enhanced by arboryx.ai] — URLs must survive the dedup hop

**Gap in the original proposal:** the "strategist prompt change" above silently assumes the strategist *has* a URL to pass. It doesn't — because the URL dies one stage earlier, in the DE→dedup handoff.

**What we observed during implementation:**
- Ran `dev-utils/inspect_scout_output.py` — confirmed the scout reliably emits canonical URLs (Bloomberg, Reuters, TechCrunch, press-release domains) in its text output, one per finding.
- But `dedup_findings(scout_findings_json, category)` had a string-only contract: `'["finding 1 text", "finding 2 text", ...]'`. The DE flattens the scout's prose into bare finding strings for this call, dropping URLs before dedup sees them.
- Result: `source_url` was `null` on every Phase-1 live-run entry despite URLs being present upstream.

**Enhancement — make dedup URL-aware:**

1. **Contract change.** `dedup_findings` now accepts an array of `{finding, source_url}` objects (with bare-string backward compat) and returns `{"kept": [...full objects...], "dropped": [...reports...]}`. URLs stay welded to findings through the filter — no LLM re-attachment, no index-bookkeeping fragility.
2. **3-layer dedup.** URL equality becomes Layer 1 (free string compare → drop with `reason: "url_match"`), TF-IDF stays Layer 2, entity overlap stays Layer 3. Historical entries with null URL simply fall through to Layer 2/3.
3. **Dropped report shape:** `{title, matched_entry_id, reason, scores: {tfidf, entity}}` — enough for audit without shipping full finding text back to the LLM.
4. **Intra-batch URL dedup** as a side benefit — scout occasionally duplicates a URL within a single sweep; caught with the same Layer-1 compare.

**Why this matters for the Robotics module:** the original proposal's correctness relies on URLs actually arriving at `append_to_memory_log`. Without this enhancement, `source_url` is always `null` in practice — every acceptance test would still pass (they only check that the *parameter* works), but real-world log entries would have no URLs. The enhancement closes the loop so the proposal's Phase-1 goal (URL presence in the master log) is actually achieved.

**Scope discipline:** Layer 2/3 scoring logic is unchanged. Only the data shape crossing the function boundary changed. One observed edge case — `_entity_overlap` returns 1.0 when a scout finding contributes only a single common entity (e.g., `AI`) that happens to appear in baseline — is pre-existing and flagged for a Phase-3 pass in `arboryx.ai/dev-utils/workbench.md`. Did not bundle a fix here to keep the change surface tight.

**Validation:** see `arboryx.ai/dev-utils/test_dedup_url.py` — 9 tests covering URL fast-path hit, URL miss → tfidf catch, novel-kept URL preservation, return shape, bare-string backward compat, URL verbatim (including query params), intra-batch URL dedup, historical entries matched via tfidf, mixed object/string input. All 9/9 pass. Full design rationale in `arboryx.ai/dev-utils/workbench.md`.

---

## `merge_sector_shards` behavior

Once all new entries write `YYYY-MM-DD` timestamps, the existing merge sort should order correctly by string comparison. No code change expected. Verify with the acceptance test below.

If pre-existing shards still contain legacy timestamps during the transition window, they will sort inconsistently — but the one-time Robotics-module corrector will normalize the master log before that matters, and shards are ephemeral anyway per the current design.

---

## Data engineer output constraints (values.yaml)

### Problem

The current `data_engineer_instructions` prompt (values.yaml:86) names the 5 allowed sentiments inline:

> `...assign a sentiment (Very Bullish/Bullish/Neutral/Bearish/Very Bearish)`

But the LLM routinely ignores the constraint. In the existing 895-entry master log, 32 entries contain strength-qualifier synonyms instead of the canonical labels:

| Variant | Count | Should be |
|---|---|---|
| Highly Bullish | 15 | Very Bullish |
| Extremely Bullish | 10 | Very Bullish |
| Strongly Bullish | 3 | Very Bullish |
| Highly Bearish | 2 | Very Bearish |
| Heavily Bullish | 1 | Very Bullish |
| Drastically Bearish | 1 | Very Bearish |

The Robotics module's corrector normalizes these for the existing log, but the fix belongs at the source. Without explicit output constraints, the LLM treats "Highly Bullish" as a creative synonym.

The strategist prompt at values.yaml:108 has a dedicated `<output_constraints>` block that works. The data engineer prompt does not. Proposal: add one.

### Recommended `<output_constraints>` block for `data_engineer_instructions`

Insert immediately after the `</task_instructions>` closing tag (currently line 89). Mirrors the strategist's structure.

```yaml
    </task_instructions>
    <output_constraints>
    - **SENTIMENT**: Assign EXACTLY ONE of these 5 values, verbatim, case-sensitive:
      - `Very Bullish`
      - `Bullish`
      - `Neutral`
      - `Bearish`
      - `Very Bearish`
      Do NOT invent synonyms (no "Highly Bullish", "Extremely Bullish", "Strongly Bullish",
      "Moderately Bearish", etc.). Strength intensifiers collapse to `Very Bullish` / `Very Bearish`.
      Mixed-signal sentiment (e.g., "short-term bearish, long-term bullish") must be resolved
      to a single label that reflects the dominant near-term read.
    - **TAKEAWAY STRUCTURE**: Exactly 3 layered takeaways, each prefixed with its layer label:
      - `Direct:` — the first-order implication for named entities
      - `Indirect:` — second-order effects on related entities or sub-sectors
      - `Market Dynamics:` — structural/flow/positioning impact
    - **FORMAT**: Emit the sentiment label on its own line at the end, after the 3 takeaways.
      Example:
      ```
      Direct: [one sentence]
      Indirect: [one sentence]
      Market Dynamics: [one sentence]
      Sentiment: Very Bullish
      ```
    - **TICKERS**: Use official exchange tickers in uppercase (e.g., `NVDA`, `AMZN`). For
      private companies, name the entity without a ticker placeholder.
    - **CONCISENESS**: Each takeaway ≤ 25 words. No hedging ("may", "could potentially").
    </output_constraints>
```

### Why this structure

- **Closed vocabulary stated twice + counter-examples**: the current prompt names the 5 options but not the forbidden variants. Listing what NOT to say is what stops the synonym drift — LLMs respect explicit bans more than enumerated allowances.
- **Fixed layer labels**: "Direct/Indirect/Market Dynamics" is already used inconsistently in the log. Locking the prefix format lets the Robotics module parse each layer deterministically later without an LLM call.
- **Sentiment as a final structured line**: downstream parsers (Robotics-module extraction, confidence scoring) can grab the sentiment with a single regex instead of scanning prose. Putting it at the end, on its own line, makes it trivially parseable.
- **Mirrors strategist's pattern**: consistent prompt architecture across the three agents lowers maintenance cost.

### Acceptance test for this change

After updating the prompt, run a week of normal Arboryx operations and grep the new entries:

```bash
jq -r '.[] | select(.timestamp >= "2026-04-18") | .sentiment_takeaways' market_findings_log.json \
  | grep -iE "\b(highly|extremely|strongly|heavily|drastically)\s+(bullish|bearish)\b"
```

Expected: zero matches. If matches appear, tighten the counter-example list in the prompt.

---

## Acceptance tests (run locally before shipping)

Write these as quick scripts or REPL checks. They are the contract the Robotics module depends on.

### Test 1 — ID uniqueness within a date

```python
# Append 3 Robotics entries dated 2026-04-15
id1 = append_to_memory_log("Robotics", "finding 1", "2026-04-15", "...", "...", "...", "https://a.com")
id2 = append_to_memory_log("Robotics", "finding 2", "2026-04-15", "...", "...", "...", None)
id3 = append_to_memory_log("Robotics", "finding 3", "2026-04-15", "...", "...", "...", "https://c.com")
assert id1 == "ROB-041526-001"
assert id2 == "ROB-041526-002"
assert id3 == "ROB-041526-003"
```

### Test 2 — Counter resets on new date

```python
# After the 3 entries above, add one on a different date
id4 = append_to_memory_log("Robotics", "finding 4", "2026-04-16", "...", "...", "...", None)
assert id4 == "ROB-041626-001"
```

### Test 3 — Counter is per-category

```python
# A Crypto entry on the same date as Robotics starts fresh
id5 = append_to_memory_log("Crypto", "finding 5", "2026-04-15", "...", "...", "...", None)
assert id5 == "CRY-041526-001"
```

### Test 4 — Timestamp normalization

```python
id6 = append_to_memory_log("Robotics", "finding 6", "March 24, 2026", "...", "...", "...", None)
entry = read_last_entry()
assert entry["timestamp"] == "2026-03-24"
assert entry["entry_id"] == "ROB-032426-001"   # assuming no prior 2026-03-24 Robotics entries
```

### Test 5 — Schema completeness

```python
entry = read_last_entry()
required = {"entry_id", "timestamp", "category", "finding", "sentiment_takeaways",
            "guidance_play", "price_levels", "source_url"}
assert set(entry.keys()) == required
```

### Test 6 — Missing source_url stores null

```python
id7 = append_to_memory_log("Robotics", "finding 7", "2026-04-17", "...", "...", "...")
entry = read_last_entry()
assert entry["source_url"] is None
```

### Test 7 — Unparseable timestamp raises

```python
try:
    append_to_memory_log("Robotics", "f", "not a date", "...", "...", "...", None)
    assert False, "should have raised"
except ValueError:
    pass
```

### Test 8 — Range-shaped timestamp rejected

```python
for bad_ts in ["March 21-22, 2026", "March 18–19, 2026", "2026-03-23 to 2026-03-27"]:
    try:
        append_to_memory_log("Robotics", "f", bad_ts, "...", "...", "...", None)
        assert False, f"should have raised for {bad_ts!r}"
    except ValueError:
        pass
```

---

## Non-goals — explicitly do not do these

- Do not rewrite historical entries in the master log from Arboryx — the Robotics module owns the one-time corrector
- Do not add a schema migration / versioning layer — this is a forward-only change
- Do not add URL validation, dedup, or enrichment for `source_url`
- Do not display `source_url` in any Arboryx user-facing output
- Do not rename or restructure `market_findings_log.json`
- Do not add a separate counter file, DB, or state store for IDs — derive from the log

---

## Deployment note

After the edits deploy:
1. The Robotics module runs its one-time corrector against the existing master log (assigns IDs, normalizes dates, normalizes sentiment variants across all 895 existing entries)
2. Arboryx resumes normal daily writes — new entries get IDs from the updated `append_to_memory_log`
3. The Robotics module's incremental ingestion reads via `entry_id > last_processed_id` filter

Order matters: corrector runs once, then Arboryx writes. If Arboryx writes a new entry before the corrector runs, the corrector's counter derivation will account for it correctly anyway (same algorithm) — but cleaner to sequence them.
