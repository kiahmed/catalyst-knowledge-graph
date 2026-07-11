"""Per-entity social handle resolution — verify-or-abstain, no LLM.

Spec: docs/handle-resolution-spec.md. Consumer: soljet-postiz (@-mentions
the subject company of each card with the correct per-platform handle).

PRIMARY path: a web-search API. One query per (entity, channel); the
handle and the company name are extracted from the top result
titles/links ("Figure | LinkedIn" + /company/figure-ai;
"Figure (@Figure_robot) / X") and accepted ONLY if the result's own
company name matches the entity (fuzzy, after stripping legal/generic
suffix tokens from both sides). No scraping, no rate-walls.
This also makes X resolvable without the paid X API.

Providers (first configured wins):
  - Serper.dev        SERPER_API_KEY                    (2,500 free queries)
  - SearchApi.io      SEARCHAPI_API_KEY                 (100 free requests)
  - Google CSE        GOOGLE_CSE_API_KEY + GOOGLE_CSE_ID (closed to new
    customers since ~2025 — kept for grandfathered projects)

FALLBACK (LinkedIn only, when CSE is unconfigured or returns nothing):
direct fetch of the public /company/<slug> page, verified against its
og:title. LinkedIn 999-walls IPs after a handful of rapid hits, so direct
fetches are spaced far apart (DIRECT_FALLBACK_DELAY_S).

Either way the rule is verify-or-abstain: a wrong @ tags the wrong real
company, so precision beats coverage.

Results (including abstains) are cached in the shared `entity_handles`
table keyed by (name_key, channel) — never per-entry — so each canonical
entity is resolved at most once and every later card/channel reuses it.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass

import httpx
from thefuzz import fuzz

log = logging.getLogger(__name__)

CHANNELS = ("linkedin", "x")

# Sources, in the spirit of the spec's handle_source column.
SOURCE_LINKEDIN = "linkedin_verified"        # direct page fetch verified
SOURCE_LINKEDIN_SERP = "linkedin_serp_verified"  # Google CSE result verified
SOURCE_X_SERP = "x_serp_verified"
SOURCE_HUMAN = "human"
SOURCE_ABSTAIN = "abstain"      # resolved: confidently no handle / ambiguous
SOURCE_BLOCKED = "blocked"      # rate-walled / quota-exhausted — retryable

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_LINKEDIN_COMPANY_URL = "https://www.linkedin.com/company/{slug}"
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]+)"')

# Tokens ignored when COMPARING names (not when building slugs): legal
# suffixes plus generic industry fillers. "Figure AI" vs page name
# "Figure" should match; "Humanoid" vs "Humanoid Global Holdings" should not.
_GENERIC_TOKENS = frozenset(
    "inc corp corporation co ltd llc gmbh ag plc sa se nv ab oyj kk"
    " ai technologies technology labs holdings group energy".split()
)

DEFAULT_MATCH_THRESHOLD = 90    # thefuzz score 0..100


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Graceful pacing — all env-tunable. Free accounts + long-running sweeps:
# be a polite guest everywhere, not just where we've already been walled.
# LinkedIn 999s an IP after a handful of rapid requests (observed: ~4 in
# 10s) and the wall stays up for hours; blocked rows are cached retryable,
# so slow-and-steady beats fast-and-blocked.
DEFAULT_FETCH_DELAY_S = _env_float("HANDLE_FETCH_DELAY_S", 8.0)     # between direct-fetch candidates
DEFAULT_FETCH_JITTER_S = _env_float("HANDLE_FETCH_JITTER_S", 4.0)
SEARCH_DELAY_S = _env_float("HANDLE_SEARCH_DELAY_S", 2.0)           # between search-API queries
# Direct LinkedIn fetches are the rare fallback when the SERP is empty —
# space them out hard so the IP never trips the wall.
DIRECT_FALLBACK_DELAY_S = _env_float("HANDLE_DIRECT_DELAY_S", 30.0)


def polite_sleep(delay_s: float = DEFAULT_FETCH_DELAY_S) -> None:
    if delay_s <= 0:        # tests pass 0 — skip the jitter too
        return
    time.sleep(delay_s + random.uniform(0, DEFAULT_FETCH_JITTER_S))


# Canonical comments for blocked results — _sweep branches on these to
# decide what a block implies for the REST of the run (a search-API outage
# affects every entity; a walled direct fetch only affects fallbacks).
COMMENT_SEARCH_UNAVAILABLE = "search API unavailable (quota/key/network) — retry later"
COMMENT_DIRECT_WALLED = "LinkedIn rate-walled (999/authwall) — retried on next sweep"
COMMENT_DIRECT_SKIPPED = "SERP empty; direct fetch skipped (IP rate-walled) — retry later"


@dataclass
class HandleResult:
    handle: str | None      # "@figure-ai" | None (abstained/blocked)
    confidence: float       # 0..1
    source: str             # SOURCE_* above
    comment: str | None = None  # human-readable why (match/reject/block reason)


# ── Name normalization ─────────────────────────────────────────────

def name_key(name: str) -> str:
    """Cache key for the shared entity_handles table."""
    return re.sub(r"\s+", " ", name).strip().lower()


def _tokens(name: str) -> list[str]:
    # Fold diacritics first ("Zenobē" → "zenobe") — the ascii filter below
    # would otherwise silently drop them and cripple fuzzy matching.
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower().replace("&", " and ")).split()


def _strip_generic(tokens: list[str]) -> list[str]:
    """Drop trailing legal/generic tokens (never strips the whole name)."""
    out = list(tokens)
    while len(out) > 1 and out[-1] in _GENERIC_TOKENS:
        out.pop()
    return out


def slug_candidates(name: str, aliases: list[str] | None = None, limit: int = 6) -> list[str]:
    """Ordered LinkedIn /company/ slug guesses from a name + its aliases."""
    seen: set[str] = set()
    out: list[str] = []
    for base in [name, *(aliases or [])]:
        toks = _tokens(base)
        if not toks:
            continue
        for cand in ("-".join(toks), "-".join(_strip_generic(toks)), "".join(toks)):
            # 1-char slugs ('a', 'x') are never the right company page.
            if len(cand) >= 2 and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out[:limit]


# ── LinkedIn fetch-verify ──────────────────────────────────────────

def _page_company_name(html: str) -> str | None:
    """Company name from og:title ('Figure | LinkedIn'), or None if authwalled."""
    m = _OG_TITLE_RE.search(html)
    if not m:
        return None
    title = m.group(1).split(" | LinkedIn")[0].strip()
    if not title or "sign up" in title.lower() or "log in" in title.lower():
        return None
    return title


def _match_score(page_name: str, known_names: list[str]) -> int:
    """Best fuzzy score of the page's company name vs any known name/alias."""
    page_stripped = " ".join(_strip_generic(_tokens(page_name)))
    best = 0
    for known in known_names:
        known_stripped = " ".join(_strip_generic(_tokens(known)))
        best = max(
            best,
            fuzz.token_sort_ratio(page_name.lower(), known.lower()),
            fuzz.token_sort_ratio(page_stripped, known_stripped),
        )
    return best


def resolve_linkedin(
    name: str,
    aliases: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    threshold: int = DEFAULT_MATCH_THRESHOLD,
    delay_s: float = DEFAULT_FETCH_DELAY_S,
) -> HandleResult:
    """Verify-or-abstain LinkedIn handle for one entity. Never guesses."""
    known_names = [name, *(aliases or [])]
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            headers={"User-Agent": _UA}, timeout=15.0, follow_redirects=True
        )
    blocked = False
    best_reject: tuple[int, str, str] | None = None   # (score, slug, page_name)
    try:
        for i, slug in enumerate(slug_candidates(name, aliases)):
            if i:
                polite_sleep(delay_s)
            try:
                resp = client.get(_LINKEDIN_COMPANY_URL.format(slug=slug))
            except httpx.HTTPError as exc:
                log.warning("linkedin fetch failed for %r (%s): %s", name, slug, exc)
                blocked = True
                continue
            if resp.status_code in (403, 429, 999):
                # Rate-limited/bot-walled: further candidates will fail too.
                blocked = True
                break
            if resp.status_code != 200:
                continue
            page_name = _page_company_name(resp.text)
            if page_name is None:       # authwall page — can't verify
                blocked = True
                continue
            score = _match_score(page_name, known_names)
            if score >= threshold:
                # Prefer the slug LinkedIn itself redirected to (canonical).
                final_slug = _slug_from_url(str(resp.url)) or slug
                log.info(
                    "linkedin verified %r -> @%s (page=%r score=%d)",
                    name, final_slug, page_name, score,
                )
                return HandleResult(
                    f"@{final_slug}", score / 100.0, SOURCE_LINKEDIN,
                    f"page '{page_name}' matched (score {score})",
                )
            if best_reject is None or score > best_reject[0]:
                best_reject = (score, slug, page_name)
            log.info(
                "linkedin candidate rejected for %r: /company/%s is %r (score=%d)",
                name, slug, page_name, score,
            )
    finally:
        if owns_client:
            client.close()
    if blocked:
        return HandleResult(None, 0.0, SOURCE_BLOCKED, COMMENT_DIRECT_WALLED)
    if best_reject:
        score, slug, page_name = best_reject
        return HandleResult(
            None, 0.0, SOURCE_ABSTAIN,
            f"no candidate verified; closest was /company/{slug} = "
            f"'{page_name}' (score {score} < {threshold})",
        )
    return HandleResult(
        None, 0.0, SOURCE_ABSTAIN, "no company page found (all candidates 404)"
    )


def _slug_from_url(url: str) -> str | None:
    """Company slug from a LinkedIn URL — None if it can't work as a text
    @-mention (percent-encoded/CJK slugs, '&', etc. would break posts)."""
    m = re.search(r"/company/([^/?#]+)", url)
    if not m:
        return None
    slug = urllib.parse.unquote(m.group(1))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug):
        return None
    return slug


# ── Web-search providers (primary path) ───────────────────────────

_CSE_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"
_SERPER_ENDPOINT = "https://google.serper.dev/search"
_SEARCHAPI_ENDPOINT = "https://www.searchapi.io/api/v1/search"
# Profile-page titles: "Figure (@Figure_robot) / X",
# "KUKA Fan (@KUKA_Robotics) / Posts / X - Twitter", "... | Twitter".
# After "(@handle)" only separator/suffix junk may follow — a tweet title
# quoting "(@somebody)" mid-sentence won't match.
_X_TITLE_RE = re.compile(r"^(.*?)\s*\(@([A-Za-z0-9_]{1,15})\)\s*(?:[/|·].*)?$", re.I)
_X_PROFILE_URL_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?(?:\?.*)?$")
_LINKEDIN_TITLE_SPLIT_RE = re.compile(r"\s*[|\-–]\s*LinkedIn.*$")


@dataclass
class CseConfig:
    api_key: str
    cx: str


@dataclass
class SerperConfig:
    api_key: str


@dataclass
class SearchApiConfig:
    api_key: str


SearchProvider = CseConfig | SerperConfig | SearchApiConfig


class SearchQuotaError(Exception):
    """Search API said no (quota exhausted / key rejected) — retryable later."""


def search_provider_from_env() -> SearchProvider | None:
    serper = os.environ.get("SERPER_API_KEY", "").strip()
    if serper:
        return SerperConfig(serper)
    searchapi = os.environ.get("SEARCHAPI_API_KEY", "").strip()
    if searchapi:
        return SearchApiConfig(searchapi)
    key = os.environ.get("GOOGLE_CSE_API_KEY", "").strip()
    cx = os.environ.get("GOOGLE_CSE_ID", "").strip()
    return CseConfig(key, cx) if key and cx else None


def _web_search(
    client: httpx.Client, search: SearchProvider, query: str, num: int = 5
) -> list[dict]:
    """Top results as [{'title', 'link'}]. Raises SearchQuotaError on
    quota/key rejection AND on network errors — both retryable, and a
    single flaky request must not kill a whole sweep batch."""
    try:
        return _web_search_raw(client, search, query, num)
    except httpx.HTTPError as exc:
        raise SearchQuotaError(f"search request failed ({type(exc).__name__}): {exc}")


def _web_search_raw(
    client: httpx.Client, search: SearchProvider, query: str, num: int = 5
) -> list[dict]:
    if isinstance(search, SerperConfig):
        resp = client.post(
            _SERPER_ENDPOINT,
            json={"q": query, "num": num},
            headers={"X-API-KEY": search.api_key, "Content-Type": "application/json"},
        )
        # Serper reports exhausted credits as 400 "Not enough credits".
        if resp.status_code in (400, 401, 403, 429):
            raise SearchQuotaError(f"Serper HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "link": r.get("link", "")}
            for r in resp.json().get("organic", []) or []
        ]
    if isinstance(search, SearchApiConfig):
        resp = client.get(
            _SEARCHAPI_ENDPOINT,
            params={"engine": "google", "q": query},
            headers={"Authorization": f"Bearer {search.api_key}"},
        )
        if resp.status_code in (401, 402, 403, 429):
            raise SearchQuotaError(f"SearchApi HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "link": r.get("link", "")}
            for r in resp.json().get("organic_results", []) or []
        ][:num]
    resp = client.get(
        _CSE_ENDPOINT,
        params={"key": search.api_key, "cx": search.cx, "q": query, "num": num},
    )
    if resp.status_code in (403, 429):
        raise SearchQuotaError(f"CSE HTTP {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json().get("items", []) or []


def resolve_linkedin_serp(
    name: str,
    aliases: list[str] | None,
    *,
    client: httpx.Client,
    search: SearchProvider,
    threshold: int = DEFAULT_MATCH_THRESHOLD,
) -> HandleResult | None:
    """LinkedIn handle from web-search results. Returns None when the SERP
    has no /company/ results at all (caller may fall back to direct fetch).
    Raises SearchQuotaError on quota/key failure."""
    known_names = [name, *(aliases or [])]
    items = _web_search(client, search, f'site:linkedin.com/company "{name}"')
    best_reject: tuple[int, str, str] | None = None
    seen_company_result = False
    for item in items[:4]:
        link, title = item.get("link", ""), item.get("title", "")
        if "/company/" not in link:
            continue
        slug = _slug_from_url(link)
        if not slug:
            continue
        seen_company_result = True
        page_name = _LINKEDIN_TITLE_SPLIT_RE.sub("", title).strip()
        if not page_name:
            continue
        score = _match_score(page_name, known_names)
        if score >= threshold:
            log.info("linkedin SERP verified %r -> @%s (title=%r score=%d)",
                     name, slug, page_name, score)
            return HandleResult(
                f"@{slug}", score / 100.0, SOURCE_LINKEDIN_SERP,
                f"SERP title '{page_name}' matched (score {score})",
            )
        if best_reject is None or score > best_reject[0]:
            best_reject = (score, slug, page_name)
    if best_reject:
        score, slug, page_name = best_reject
        return HandleResult(
            None, 0.0, SOURCE_ABSTAIN,
            f"no SERP result verified; closest was /company/{slug} = "
            f"'{page_name}' (score {score} < {threshold})",
        )
    if seen_company_result:
        return HandleResult(None, 0.0, SOURCE_ABSTAIN, "SERP company results unusable")
    return None    # nothing on the SERP — direct fetch may still find it


def resolve_x_serp(
    name: str,
    aliases: list[str] | None,
    *,
    client: httpx.Client,
    search: SearchProvider,
    threshold: int = DEFAULT_MATCH_THRESHOLD,
) -> HandleResult:
    """X handle from web-search results — profile pages only, display name
    must match the entity. Raises SearchQuotaError on quota/key failure."""
    known_names = [name, *(aliases or [])]
    items = _web_search(client, search, f'site:x.com "{name}"')
    best_reject: tuple[int, str, str] | None = None
    for item in items[:4]:
        link, title = item.get("link", ""), item.get("title", "")
        url_m = _X_PROFILE_URL_RE.match(link)
        title_m = _X_TITLE_RE.match(title)
        if not title_m or not url_m:
            continue    # tweets (/status/ links) and non-profile pages
        display_name, handle = title_m.group(1).strip(), title_m.group(2)
        if url_m.group(1).lower() != handle.lower():
            continue    # title/URL disagree — not a profile page, skip
        score = _match_score(display_name, known_names)
        if score >= threshold:
            log.info("x SERP verified %r -> @%s (display=%r score=%d)",
                     name, handle, display_name, score)
            return HandleResult(
                f"@{handle}", score / 100.0, SOURCE_X_SERP,
                f"SERP profile '{display_name}' matched (score {score})",
            )
        if best_reject is None or score > best_reject[0]:
            best_reject = (score, handle, display_name)
    if best_reject:
        score, handle, display_name = best_reject
        return HandleResult(
            None, 0.0, SOURCE_ABSTAIN,
            f"no SERP profile verified; closest was @{handle} = "
            f"'{display_name}' (score {score} < {threshold})",
        )
    return HandleResult(None, 0.0, SOURCE_ABSTAIN, "no X profile found on SERP")


# ── Channel dispatch ───────────────────────────────────────────────

def resolve_channel(
    channel: str,
    name: str,
    aliases: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    search: SearchProvider | None = None,
    allow_direct: bool = True,
) -> HandleResult:
    """Resolve one (entity, channel). Web search is primary when configured;
    direct LinkedIn fetch is the well-spaced fallback (skippable via
    allow_direct once a sweep learns the IP is walled). X needs a provider."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            headers={"User-Agent": _UA}, timeout=15.0, follow_redirects=True
        )
    try:
        if channel == "linkedin":
            if search:
                try:
                    result = resolve_linkedin_serp(name, aliases, client=client, search=search)
                except SearchQuotaError as exc:
                    log.warning("search failure: %s", exc)
                    return HandleResult(
                        None, 0.0, SOURCE_BLOCKED, COMMENT_SEARCH_UNAVAILABLE,
                    )
                if result is not None:
                    return result
                if not allow_direct:
                    return HandleResult(None, 0.0, SOURCE_BLOCKED, COMMENT_DIRECT_SKIPPED)
                # SERP empty → rare direct fallback, heavily spaced.
                polite_sleep(DIRECT_FALLBACK_DELAY_S)
            return resolve_linkedin(name, aliases, client=client)
        if channel == "x" and search:
            try:
                return resolve_x_serp(name, aliases, client=client, search=search)
            except SearchQuotaError as exc:
                log.warning("search failure: %s", exc)
                return HandleResult(
                    None, 0.0, SOURCE_BLOCKED, COMMENT_SEARCH_UNAVAILABLE,
                )
        return HandleResult(
            None, 0.0, SOURCE_ABSTAIN,
            f"{channel} is not auto-resolvable without a search API — set manually "
            "(make handles-set) or configure SERPER_API_KEY",
        )
    finally:
        if owns_client:
            client.close()
