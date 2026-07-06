"""Read-only sanity checker for Arboryx Firestore findings.

Normalizes timestamps to YYYY-MM-DD in memory, assigns canonical entry_ids,
and re-sorts newest-first so the validator can flag duplicates, out-of-order
timestamps, malformed IDs, and missing fields. Idempotent: running twice
produces identical output.

Usage:
    # Read-only check against Arboryx Firestore (default mode)
    python dev-utils/master_log_corrector.py --dry-run

    # Inspect a local JSON snapshot, no Firestore involvement
    python dev-utils/master_log_corrector.py --local /path/to/log.json

Note: --apply (write-back) was retired with the Firestore cutover on
2026-05-10. Arboryx is the single source of truth for findings, and any
correction belongs in Arboryx's own tooling — not this downstream consumer.
If you spot a problem here, report it upstream.

Dependencies: google-cloud-firestore, python-dateutil
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as date_parser

DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "")
DEFAULT_DATABASE = "(default)"
DEFAULT_COLLECTION = "findings"

CATEGORY_ABBR = {
    "Robotics": "ROB",
    "Crypto": "CRY",
    "AI Stack": "AIS",
    "Space & Defense": "SPD",
    "Power & Energy": "PWE",
    "Strategic Minerals": "STM",
}

CANONICAL_FIELDS = (
    "entry_id",
    "timestamp",
    "category",
    "finding",
    "sentiment_takeaways",
    "guidance_play",
    "price_levels",
    "source_url",
)

# Canonical sentiment vocabulary per values.yaml:86 (data_engineer_instructions).
# The LLM sometimes emits strength-qualifier synonyms (extremely/highly/strongly
# bullish etc.); these collapse to the Very Bullish / Very Bearish extreme.
# Ordered: stronger qualifier first so "Very Extremely Bullish" (if it ever appears)
# wouldn't double-match. Case-insensitive; replacement preserves canonical capitalization.
_STRENGTH_VARIANTS = "extremely|highly|strongly|heavily|drastically"
SENTIMENT_NORMALIZATIONS = [
    (re.compile(rf"\b(?:{_STRENGTH_VARIANTS})\s+bullish\b", re.IGNORECASE), "Very Bullish"),
    (re.compile(rf"\b(?:{_STRENGTH_VARIANTS})\s+bearish\b", re.IGNORECASE), "Very Bearish"),
]

# Label alternation shared across the promotion regexes. Order matters: the
# strength-qualified alternatives must be listed before the bare ones so they
# match greedily (e.g. "highly bullish" before "bullish").
_CANONICAL_LABEL_ALT = (
    rf"(?:{_STRENGTH_VARIANTS})\s+(?:bullish|bearish)"
    r"|very\s+bullish|very\s+bearish|bullish|bearish|neutral"
)

# Pipes separating structural sections of sentiment_takeaways. Arboryx's
# data_engineer prompt occasionally emits `|` between Direct/Indirect/Market
# Dynamics/Sentiment blocks; treat them as sentence terminators.
_PIPE_SECTION_RE = re.compile(
    r"\s*\|\s*(?=(?:Direct|Indirect|Market Dynamics|Sentiment)\s*:)",
    re.IGNORECASE,
)

# Any explicit "Sentiment: <label>" fragment. Used to strip the wrapper and
# lift the bare label to the front.
_SENTIMENT_ANY_RE = re.compile(
    rf"\bSentiment\s*:\s*({_CANONICAL_LABEL_ALT})\s*\.?\s*",
    re.IGNORECASE,
)

# Trailing bare canonical label (optional emoji/period/whitespace tail).
_TRAILING_CANONICAL_RE = re.compile(
    rf"[\s.]+({_CANONICAL_LABEL_ALT})\s*[🟢🔴🟡]?\s*\.?\s*$",
    re.IGNORECASE,
)

# Leading canonical label, used to detect entries that already open in the
# canonical `<Label> ...` form so we don't double-prepend.
_LEADING_CANONICAL_RE = re.compile(
    rf"^\s*(?:{_CANONICAL_LABEL_ALT})\b",
    re.IGNORECASE,
)

# Fuzzy-prefix inference table. Each row is (regex, inferred_canonical_label,
# strip_match). Walked in order on the start of the text; first match wins.
#
#   strip_match=True   — match consumes a prefix like `Highly positive.` that's
#                        a standalone declaration; we remove it entirely and
#                        replace with the canonical label.
#   strip_match=False  — match confirms a prepositional phrase (`Positive for X`,
#                        `Mixed (...)`) whose prose carries domain info; we
#                        keep the prose intact and only prepend the label.
#
# The "standalone" regexes all end with a period so that a mid-sentence word
# like `constructive policy` doesn't accidentally fire. The "phrase" regexes
# anchor on a preposition/connector that signals the fuzzy word is the
# sentiment for the phrase that follows, not a neutral adjective.
_FUZZY_PREFIX_PATTERNS: list[tuple[re.Pattern, str, bool]] = [
    # Standalone declarations (strip).
    (re.compile(r"^\s*highly\s+positive\s*\.\s*", re.IGNORECASE), "Very Bullish", True),
    (re.compile(r"^\s*positive\s+sentiment\s*\.\s*", re.IGNORECASE), "Bullish", True),
    (re.compile(r"^\s*positive\s*\.\s*", re.IGNORECASE), "Bullish", True),
    (re.compile(r"^\s*constructive\s*\.\s*", re.IGNORECASE), "Bullish", True),
    (re.compile(r"^\s*highly\s+negative\s*\.\s*", re.IGNORECASE), "Very Bearish", True),
    (re.compile(r"^\s*negative\s+sentiment\s*\.\s*", re.IGNORECASE), "Bearish", True),
    (re.compile(r"^\s*negative\s*\.\s*", re.IGNORECASE), "Bearish", True),
    # Phrase openings (keep prose, only prepend).
    (re.compile(r"^\s*highly\s+positive\s+(?:for|on|signal|tailwinds?)\b", re.IGNORECASE), "Very Bullish", False),
    (re.compile(r"^\s*positive\s+(?:sentiment\s+)?(?:for|on|signal|tailwinds?)\b", re.IGNORECASE), "Bullish", False),
    (re.compile(r"^\s*positive\s+signal\b", re.IGNORECASE), "Bullish", False),
    (re.compile(r"^\s*constructive\s+(?:for|on|in)\b", re.IGNORECASE), "Bullish", False),
    (re.compile(r"^\s*highly\s+negative\s+(?:for|on|signal|headwinds?)\b", re.IGNORECASE), "Very Bearish", False),
    (re.compile(r"^\s*negative\s+(?:sentiment\s+)?(?:for|on|signal|headwinds?)\b", re.IGNORECASE), "Bearish", False),
    # Mixed / volatile / speculative — canonical Neutral. Parenthetical detail
    # (e.g. `Mixed (Bullish on Utility, Bearish on Legacy)`) is retained.
    (re.compile(r"^\s*mixed\s*(?:\([^)]*\))?\s*[.,]", re.IGNORECASE), "Neutral", False),
    (re.compile(r"^\s*volatile\s*(?:/\s*actionable)?\s*[.,]", re.IGNORECASE), "Neutral", False),
    (re.compile(r"^\s*highly\s+speculative\s*(?:/\s*volatile)?\s*[.,]", re.IGNORECASE), "Neutral", False),
]

# Qualifier + canonical label opening a sentence — e.g. `Macro Bullish.`. Limited
# to a 1–2 word qualifier so we don't mistake a sentence whose subject happens
# to be long (e.g. `The massive earnings beat was bullish for ...` — no period
# before `for`, so no match).
_QUALIFIED_LABEL_RE = re.compile(
    rf"^\s*(?:[A-Za-z][\w-]*\s+){{1,2}}({_CANONICAL_LABEL_ALT})\s*\.\s*",
    re.IGNORECASE,
)


def _canonicalize_label(raw: str) -> str:
    """Map a matched sentiment label string to one of the five canonical labels."""
    s = raw.strip().lower()
    if re.match(rf"(?:{_STRENGTH_VARIANTS})\s+bullish$", s):
        return "Very Bullish"
    if re.match(rf"(?:{_STRENGTH_VARIANTS})\s+bearish$", s):
        return "Very Bearish"
    mapping = {
        "very bullish": "Very Bullish",
        "very bearish": "Very Bearish",
        "bullish": "Bullish",
        "bearish": "Bearish",
        "neutral": "Neutral",
    }
    return mapping.get(s, "Neutral")


def _starts_with_canonical(text: str) -> bool:
    return bool(_LEADING_CANONICAL_RE.match(text))


def _prepend_label(label: str, body: str) -> str:
    """Prepend `<Label>.` to body. Output: `<Label>. <body>` with a single space."""
    body = body.strip()
    if not body:
        return f"{label}."
    return f"{label}. {body}"


def _normalize_pipe_separators(text: str) -> tuple[str, bool]:
    """Replace structural-section pipes with `. ` (period-space) and collapse
    any runs of 2+ consecutive periods (ellipses, or double-periods introduced
    when the section already ended in a period ahead of the pipe) to a single
    period. Idempotent: after one pass, no section-terminating pipes and no
    period runs remain. The bool return flags only pipe replacement — ellipsis
    collapse is a silent cleanup."""
    after_pipe = _PIPE_SECTION_RE.sub(". ", text)
    piped = after_pipe != text
    new = re.sub(r"\.{2,}(\s+|$)", r".\1", after_pipe)
    return new, piped


def _promote_explicit_sentiment(text: str) -> tuple[str, str | None]:
    """Strip every `Sentiment: X` fragment and prepend the bare canonical label.

    Returns (new_text, label_or_none). Idempotent: second-pass input starts with
    a bare `<Label>.` so `_SENTIMENT_ANY_RE` won't match and the function no-ops.
    """
    m = _SENTIMENT_ANY_RE.search(text)
    if not m:
        return text, None
    label = _canonicalize_label(m.group(1))
    stripped = _SENTIMENT_ANY_RE.sub("", text).strip()
    # Tidy orphaned whitespace before punctuation left by the strip.
    stripped = re.sub(r"\s+([.!?])", r"\1", stripped)
    # If the remainder also opens with a bare canonical label, leave that alone
    # — prepending would double-label the sentence.
    if _starts_with_canonical(stripped):
        return stripped, label
    return _prepend_label(label, stripped), label


def _promote_trailing_canonical(text: str) -> tuple[str, str | None]:
    """Lift a bare trailing canonical label (e.g. `... accumulation. Bullish.`
    or `... applications. Very Bullish 🟢`) to the front. No-op if the text
    already opens with a canonical label."""
    if _starts_with_canonical(text):
        return text, None
    m = _TRAILING_CANONICAL_RE.search(text)
    if not m:
        return text, None
    label = _canonicalize_label(m.group(1))
    body = text[: m.start()].rstrip()
    if not body:
        return f"{label}.", label
    if not body.endswith((".", "!", "?")):
        body += "."
    return _prepend_label(label, body), label


def _promote_fuzzy_prefix(text: str) -> tuple[str, str | None]:
    """Infer a canonical label from a fuzzy opening phrase. Tries, in order:

    1. `_FUZZY_PREFIX_PATTERNS` — mapping of opening phrases to canonical
       labels (standalone declarations get stripped; phrase openings keep
       their prose).
    2. `_QUALIFIED_LABEL_RE` — `<qualifier> <canonical label>.` (e.g.
       `Macro Bullish.`) — strip the whole first sentence.

    No-op if the text already opens with a canonical label.
    """
    if _starts_with_canonical(text):
        return text, None

    for pattern, label, strip in _FUZZY_PREFIX_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        if strip:
            remainder = text[m.end():].lstrip()
            return _prepend_label(label, remainder), label
        return _prepend_label(label, text.lstrip()), label

    m_q = _QUALIFIED_LABEL_RE.match(text)
    if m_q:
        label = _canonicalize_label(m_q.group(1))
        remainder = text[m_q.end():].lstrip()
        return _prepend_label(label, remainder), label

    return text, None


def normalize_sentiment(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace non-canonical strength qualifiers with canonical sentiment labels.

    Returns (cleaned_text, applied) where applied is a list of (matched, replacement)
    tuples for auditing. Only touches the 5 strength-synonym patterns; leaves all
    other prose (e.g. "short-term bearish", "neutral to bullish") untouched.
    """
    if not text:
        return text, []
    applied: list[tuple[str, str]] = []
    out = text
    for pattern, replacement in SENTIMENT_NORMALIZATIONS:
        def _sub(m: re.Match) -> str:
            applied.append((m.group(0), replacement))
            return replacement
        out = pattern.sub(_sub, out)
    return out, applied


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]")
# Digit-hyphen-digit (e.g. "March 21-22, 2026") — only triggers if not a strict ISO date
_DIGIT_HYPHEN_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
# Ordered so multi-char separators are matched before bare " - "
_RANGE_SEPS = ("–", "—", " to ", " - ")


def _split_range(s: str) -> tuple[str, str] | None:
    """Return (left, right) if s contains a date-range indicator, else None."""
    for sep in _RANGE_SEPS:
        if sep in s:
            left, right = s.split(sep, 1)
            return left.strip(), right.strip()
    m = _DIGIT_HYPHEN_RE.search(s)
    if m:
        hyphen_idx = s.find("-", m.start())
        return s[:hyphen_idx].strip(), s[hyphen_idx + 1:].strip()
    return None


def normalize_timestamp(raw: str | None) -> tuple[str, bool]:
    """Normalize a timestamp to YYYY-MM-DD. Returns (iso_date, was_range).

    Ranges collapse to the earlier (start) date by convention. This is a
    one-time legacy affordance for the original 36 entries that had
    range-shaped timestamps; Arboryx's append_to_memory_log raises on
    range-shaped inputs going forward.
    """
    if raw is None or not str(raw).strip():
        raise ValueError("timestamp required")
    s = str(raw).strip()

    if _ISO_DATE_RE.match(s):
        return s, False
    if _ISO_DATETIME_RE.match(s):
        return s[:10], False

    split = _split_range(s)
    if split is not None:
        left, right = split
        try:
            right_parsed = date_parser.parse(right)
        except (date_parser.ParserError, ValueError, OverflowError):
            right_parsed = None
        try:
            if right_parsed is not None:
                left_parsed = date_parser.parse(left, default=right_parsed)
            else:
                left_parsed = date_parser.parse(left)
            return left_parsed.strftime("%Y-%m-%d"), True
        except (date_parser.ParserError, ValueError, OverflowError) as ex:
            raise ValueError(f"unparseable range timestamp: {raw!r}") from ex

    try:
        return date_parser.parse(s).strftime("%Y-%m-%d"), False
    except (date_parser.ParserError, ValueError, OverflowError) as ex:
        raise ValueError(f"unparseable timestamp: {raw!r}") from ex


def entry_id_for(category: str, date_iso: str, nth: int) -> str:
    if category not in CATEGORY_ABBR:
        raise ValueError(
            f"unknown category {category!r}; add to CATEGORY_ABBR (and to docs/proposed_arboryx_changes.md)"
        )
    mm, dd, yy = date_iso[5:7], date_iso[8:10], date_iso[2:4]
    return f"{CATEGORY_ABBR[category]}-{mm}{dd}{yy}-{nth:03d}"


def _restructure_sentiment_text(text: str) -> tuple[str, dict[str, str | bool]]:
    """Apply layout transformations to a sentiment_takeaways string:
      1. pipe-separated structural sections → newlines
      2. misplaced `Sentiment: X` fragment → lifted to front
      3. trailing bare canonical label → lifted to front
      4. fuzzy `Positive/Negative for X` prose prefix → inferred `Sentiment: X.`

    Returns (new_text, actions) where actions records which transformations fired
    and which label was chosen (for stats + residual audit).
    """
    actions: dict[str, str | bool] = {
        "pipe": False, "explicit": None, "trailing": None, "fuzzy": None,
    }
    if not text:
        return text, actions
    out, piped = _normalize_pipe_separators(text)
    actions["pipe"] = piped
    out, lbl = _promote_explicit_sentiment(out)
    if lbl is not None:
        actions["explicit"] = lbl
    else:
        out, lbl = _promote_trailing_canonical(out)
        if lbl is not None:
            actions["trailing"] = lbl
        else:
            out, lbl = _promote_fuzzy_prefix(out)
            if lbl is not None:
                actions["fuzzy"] = lbl
    return out, actions


def canonicalize(entry: dict, entry_id: str, date_iso: str) -> tuple[dict, list[tuple[str, str]], dict]:
    restructured, actions = _restructure_sentiment_text(entry.get("sentiment_takeaways", ""))
    cleaned_sentiment, applied = normalize_sentiment(restructured)
    out = {
        "entry_id": entry_id,
        "timestamp": date_iso,
        "category": entry["category"],
        "finding": entry.get("finding", ""),
        "sentiment_takeaways": cleaned_sentiment,
        "guidance_play": entry.get("guidance_play", ""),
        "price_levels": entry.get("price_levels", ""),
        "source_url": entry.get("source_url"),
    }
    extras = set(entry.keys()) - set(CANONICAL_FIELDS)
    if extras:
        print(f"  warn: dropping unexpected fields {sorted(extras)} on {entry_id}", file=sys.stderr)
    return out, applied, actions


def correct_log(entries: list[dict]) -> tuple[list[dict], dict]:
    """Pure function: normalize, sort newest-first, assign IDs. Returns (corrected, stats)."""
    stats = {
        "input_count": len(entries),
        "timestamp_reformat": 0,
        "ranges_collapsed": 0,
        "id_assigned": 0,
        "id_changed": 0,
        "sentiment_normalizations": 0,
        "sentiment_replacements": Counter(),
        "unknown_categories": Counter(),
        "range_samples": [],
        "pipe_normalizations": 0,
        "sentiment_promoted_explicit": 0,
        "sentiment_promoted_trailing": 0,
        "sentiment_inferred_fuzzy": 0,
        "sentiment_residual_ids": [],
        "sentiment_promotion_labels": Counter(),
    }

    # Pass 1: normalize timestamps, surface unknown categories
    normalized: list[tuple[int, dict, str]] = []  # (original_idx, entry, date_iso)
    for idx, entry in enumerate(entries):
        raw_ts = entry.get("timestamp")
        date_iso, was_range = normalize_timestamp(raw_ts)
        if date_iso != raw_ts:
            stats["timestamp_reformat"] += 1
        if was_range:
            stats["ranges_collapsed"] += 1
            if len(stats["range_samples"]) < 10:
                stats["range_samples"].append((raw_ts, date_iso))
        cat = entry.get("category")
        if cat not in CATEGORY_ABBR:
            stats["unknown_categories"][cat] += 1
        normalized.append((idx, entry, date_iso))

    if stats["unknown_categories"]:
        unknown = dict(stats["unknown_categories"])
        raise ValueError(f"unknown categories in log: {unknown}")

    # Pass 2: stable sort newest-first by timestamp; original index breaks ties
    # (preserves input order within same date)
    normalized.sort(key=lambda t: (t[2], -t[0]), reverse=True)

    # Pass 3: assign IDs by scanning sorted list; counter per (category, date)
    counters: Counter = Counter()
    corrected: list[dict] = []
    for _, entry, date_iso in normalized:
        cat = entry["category"]
        counters[(cat, date_iso)] += 1
        new_id = entry_id_for(cat, date_iso, counters[(cat, date_iso)])
        old_id = entry.get("entry_id")
        if old_id is None:
            stats["id_assigned"] += 1
        elif old_id != new_id:
            stats["id_changed"] += 1
        out_entry, applied, actions = canonicalize(entry, new_id, date_iso)
        if applied:
            stats["sentiment_normalizations"] += 1
            for matched, repl in applied:
                stats["sentiment_replacements"][(matched.lower(), repl)] += 1
        if actions.get("pipe"):
            stats["pipe_normalizations"] += 1
        if actions.get("explicit"):
            stats["sentiment_promoted_explicit"] += 1
            stats["sentiment_promotion_labels"][("explicit", actions["explicit"])] += 1
        if actions.get("trailing"):
            stats["sentiment_promoted_trailing"] += 1
            stats["sentiment_promotion_labels"][("trailing", actions["trailing"])] += 1
        if actions.get("fuzzy"):
            stats["sentiment_inferred_fuzzy"] += 1
            stats["sentiment_promotion_labels"][("fuzzy", actions["fuzzy"])] += 1
        # Residual: sentiment_takeaways that doesn't open with one of the five
        # canonical labels after all transformations. These need manual review —
        # we don't fabricate a label.
        st = (out_entry.get("sentiment_takeaways") or "").lstrip()
        if st and not _starts_with_canonical(st):
            stats["sentiment_residual_ids"].append(new_id)
        corrected.append(out_entry)

    return corrected, stats


def verify_idempotent(corrected: list[dict]) -> None:
    """Run correct_log on its own output; must produce identical result."""
    again, _ = correct_log([dict(e) for e in corrected])
    if again != corrected:
        raise AssertionError("corrector is not idempotent — output differs on second pass")


def summarize(stats: dict, corrected: list[dict]) -> None:
    cat_counts = Counter(e["category"] for e in corrected)
    date_range = (
        min(e["timestamp"] for e in corrected),
        max(e["timestamp"] for e in corrected),
    ) if corrected else (None, None)

    print("=" * 60)
    print("Master log correction summary")
    print("=" * 60)
    print(f"  Entries in:             {stats['input_count']}")
    print(f"  Entries out:            {len(corrected)}")
    print(f"  Timestamps reformatted: {stats['timestamp_reformat']}")
    print(f"  Date ranges collapsed:  {stats['ranges_collapsed']}")
    if stats["range_samples"]:
        print(f"  Range → start-date samples:")
        for raw, iso in stats["range_samples"]:
            print(f"    {raw!r:<40} -> {iso}")
    print(f"  IDs newly assigned:     {stats['id_assigned']}")
    print(f"  IDs changed:            {stats['id_changed']}")
    print(f"  Sentiment normalizations: {stats['sentiment_normalizations']} entries, "
          f"{sum(stats['sentiment_replacements'].values())} replacements")
    if stats["sentiment_replacements"]:
        print(f"  Sentiment replacement breakdown:")
        for (matched, repl), n in stats["sentiment_replacements"].most_common():
            print(f"    {n:>4}x  {matched!r} -> {repl!r}")
    print(f"  Pipe-separator fixes:   {stats['pipe_normalizations']}")
    print(f"  Sentiment promoted (explicit `Sentiment: X`): {stats['sentiment_promoted_explicit']}")
    print(f"  Sentiment promoted (trailing bare label):    {stats['sentiment_promoted_trailing']}")
    print(f"  Sentiment inferred from fuzzy prose prefix:  {stats['sentiment_inferred_fuzzy']}")
    if stats["sentiment_promotion_labels"]:
        print(f"  Promotion breakdown (source, label → count):")
        for (src, lbl), n in stats["sentiment_promotion_labels"].most_common():
            print(f"    {n:>4}x  {src:<9} -> {lbl}")
    residual = stats["sentiment_residual_ids"]
    print(f"  Entries without `Sentiment:` marker (manual review): {len(residual)}")
    if residual:
        for rid in residual:
            print(f"    - {rid}")
    print(f"  Date range:           {date_range[0]} → {date_range[1]}")
    print(f"  Per-category counts:")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {CATEGORY_ABBR.get(cat, '???')}  {cat:<20} {n}")
    if corrected:
        print("\n  First entry (newest):")
        print(f"    {corrected[0]['entry_id']}  {corrected[0]['timestamp']}  {corrected[0]['category']}")
        print(f"    {corrected[0]['finding'][:100]}...")
        print("  Last entry (oldest):")
        print(f"    {corrected[-1]['entry_id']}  {corrected[-1]['timestamp']}  {corrected[-1]['category']}")


def load_local(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def save_local(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def firestore_download(project: str, database: str, collection: str) -> list[dict]:
    """Stream every doc in `collection` for the active project. Read-only."""
    from google.cloud import firestore
    kwargs: dict[str, object] = {"project": project} if project else {}
    if database and database != "(default)":
        kwargs["database"] = database
    client = firestore.Client(**kwargs)
    out: list[dict] = []
    for doc in client.collection(collection).stream():
        d = doc.to_dict() or {}
        d.pop("_synced_at", None)
        d.pop("_hash", None)
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Read + check, no writes (default behavior)")
    ap.add_argument("--local", type=Path, help="Process a local JSON snapshot instead of Firestore")
    ap.add_argument("--output", type=Path, help="Write the canonicalized view to a local path for diffing")
    ap.add_argument("--project", default=DEFAULT_PROJECT)
    ap.add_argument("--database", default=DEFAULT_DATABASE)
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    args = ap.parse_args()

    if args.local:
        entries = load_local(args.local)
        source_desc = str(args.local)
    else:
        entries = firestore_download(args.project, args.database, args.collection)
        source_desc = f"firestore://{args.project}/{args.database}/{args.collection}"

    print(f"Loaded {len(entries)} entries from {source_desc}")
    corrected, stats = correct_log(entries)
    verify_idempotent(corrected)
    summarize(stats, corrected)

    if args.output:
        save_local(args.output, corrected)
        print(f"\nWrote corrected view to {args.output}")

    print("\n(read-only: write-back was retired with the Firestore cutover. "
          "Report upstream issues to Arboryx.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
