"""Pass-1 enrichment: fetch article body from entry.source_url.

Called only when entry.source_url is present AND cfg.extraction.url_fetch.enabled.
Returns the main article text (trafilatura-extracted, trimmed to max_chars).

Failures are soft: on timeout / 4xx/5xx / parse failure, return empty text
and let the extractor fall back to the raw finding + optional grounding.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import trafilatura

from src.config import UrlFetchConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    text: str          # empty on failure
    title: str         # empty on failure
    ok: bool
    error: str         # one-line reason when ok=False


def fetch_article(url: str, cfg: UrlFetchConfig) -> FetchResult:
    if not cfg.enabled:
        return FetchResult(text="", title="", ok=False, error="url_fetch disabled")
    if not url:
        return FetchResult(text="", title="", ok=False, error="no url")

    headers = {"User-Agent": cfg.user_agent}
    try:
        with httpx.Client(
            timeout=cfg.timeout_s, follow_redirects=True, headers=headers
        ) as cli:
            resp = cli.get(url)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as exc:
        log.info("fetch_fail url=%s err=%s", url, exc)
        return FetchResult(text="", title="", ok=False, error=str(exc))

    extracted = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_recall=False
    ) or ""
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta else "") or ""

    text = extracted[: cfg.max_chars]
    if not text.strip():
        return FetchResult(text="", title=title, ok=False, error="empty_extract")

    log.info(
        "fetch_ok url=%s chars=%d title=%r",
        url, len(text), (title[:60] + "…") if len(title) > 60 else title,
    )
    return FetchResult(text=text, title=title, ok=True, error="")
