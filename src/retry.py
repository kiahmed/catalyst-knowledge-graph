"""Retry helper for transient Gemini / network errors.

Used by src/extract.py to wrap `client.models.generate_content` calls so a
single 429 / 5xx / timeout doesn't kill an ingest run. Keeps the contract
simple: on success, return whatever the callable returns; on final failure,
re-raise the last exception.

Transient classification is deliberately broad: any exception whose repr
contains an HTTP status in {408, 409, 429, 500, 502, 503, 504} or whose
type name matches {ServerError, ClientError (for 429), ResourceExhausted,
DeadlineExceeded, ServiceUnavailable, TimeoutError, ConnectionError} is
retried. Everything else re-raises immediately — bad API key, malformed
request, quota-exceeded-permanent, etc. should not loop.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

log = logging.getLogger("robotics.retry")

T = TypeVar("T")

# HTTP codes we treat as transient (worth retrying).
_RETRY_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

# Substrings in exception repr / message that identify transient failures.
_RETRY_KEYWORDS = (
    "resourceexhausted",
    "deadlineexceeded",
    "serviceunavailable",
    "internalservererror",
    "unavailable",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
)

# Exception class names (case-insensitive substring match on type(exc).__name__).
_RETRY_TYPE_NAMES = (
    "serverError",
    "resourceExhausted",
    "deadlineExceeded",
    "serviceUnavailable",
    "timeoutError",
    "connectionError",
    "connectionResetError",
    "remoteProtocolError",
    "readTimeout",
    "writeTimeout",
    "connectTimeout",
)


def _is_transient(exc: BaseException) -> bool:
    """Best-effort classification of 'retryable' errors."""
    name = type(exc).__name__.lower()
    for needle in _RETRY_TYPE_NAMES:
        if needle.lower() in name:
            return True

    # genai ClientError for 429 (rate limit) should retry; 400 should not.
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if code is not None and int(code) in _RETRY_HTTP_CODES:
            return True
    except (TypeError, ValueError):
        pass

    # Fallback: scan the message.
    msg = (str(exc) or "").lower()
    for needle in _RETRY_KEYWORDS:
        if needle in msg:
            return True
    for c in _RETRY_HTTP_CODES:
        if f" {c} " in f" {msg} " or f"[{c}]" in msg or f"({c})" in msg:
            return True
    return False


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.5,
    max_delay_s: float = 30.0,
    op: str = "gemini",
    context: dict | None = None,
) -> T:
    """Invoke `fn()` with exponential backoff on transient errors.

    - max_attempts: total attempts including the first (3 = 1 try + 2 retries)
    - base_delay_s: initial backoff
    - max_delay_s: cap per-attempt sleep
    - op / context: surfaced in log lines so failures are greppable by entry_id

    On exhaustion of retries or on a non-transient exception, re-raises.
    """
    ctx = context or {}
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — we classify below
            transient = _is_transient(exc)
            retryable = transient and attempt < max_attempts
            log.warning(
                "retry_attempt op=%s attempt=%d/%d transient=%s retry=%s err=%s ctx=%s",
                op, attempt, max_attempts, transient, retryable,
                f"{type(exc).__name__}: {exc}",
                ctx,
            )
            if not retryable:
                raise
            # Exponential backoff with full jitter.
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            delay = random.uniform(0.5 * delay, delay)
            time.sleep(delay)
    # Unreachable — loop either returns or re-raises.
    raise RuntimeError("call_with_retry: loop exited without return")
