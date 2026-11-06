"""Rate limiting and retry logic for YouTube Data API calls.

The YouTube Data API enforces per-user rate limits and a daily quota. Hammering
it can produce ``403`` responses (``rateLimitExceeded``/``userRateLimitExceeded``
or ``quotaExceeded``), ``429`` responses, or transient ``5xx`` errors.

This module provides:

* :class:`RateLimiter` — enforces a minimum interval between requests.
* :func:`execute_request` — executes a ``googleapiclient`` request with
  rate limiting, exponential backoff + jitter, and structured error handling.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

from googleapiclient.errors import HttpError

from ytmgr.config import (
    BACKOFF_BASE,
    BACKOFF_MAX,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL,
)
from ytmgr.errors import YouTubeAPIError

logger = logging.getLogger("ytmgr.api")

# HTTP statuses that are worth retrying.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Google error reasons that indicate a transient limit (retry after backoff).
RETRYABLE_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
    "internalError",
}

# Reasons that will not resolve by retrying soon.
NON_RETRYABLE_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "forbidden",
    "insufficientPermissions",
}

RetryCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Enforce a minimum interval between successive calls.

    This is a simple, thread-safe "leaky bucket" that guarantees at least
    ``min_interval`` seconds between the *start* of consecutive requests.
    """

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last_request: float = 0.0
        import threading

        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until it is safe to issue the next request."""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            delay = self.min_interval - elapsed
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()


# ---------------------------------------------------------------------------
# Error parsing helpers
# ---------------------------------------------------------------------------


def _parse_error(exc: HttpError) -> tuple[Optional[str], Optional[str]]:
    """Extract (reason, detail) from a googleapiclient HttpError."""
    reason: Optional[str] = None
    detail: Optional[str] = None

    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content.decode("utf-8", errors="replace"))
            error = payload.get("error", {})
            detail = error.get("message") or detail
            errors = error.get("errors") or []
            if errors and isinstance(errors, list):
                reason = errors[0].get("reason") or reason
                detail = errors[0].get("message") or detail
            # Some responses nest status under "status".
            if not reason and error.get("status"):
                reason = error["status"]
        except Exception:
            # Fall back to a truncated raw body.
            try:
                detail = content.decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = None

    if detail is None:
        detail = str(exc)

    return reason, detail


def _is_retryable(status: Optional[int], reason: Optional[str]) -> bool:
    if reason in NON_RETRYABLE_REASONS:
        return False
    if reason in RETRYABLE_REASONS:
        return True
    if status in RETRYABLE_STATUS:
        return True
    # A bare 403 without a known reason is ambiguous; do not hammer it.
    return False


def _retry_after_seconds(exc: Optional[HttpError]) -> Optional[float]:
    """Honour a ``Retry-After`` header if the server provided one."""
    if exc is None:
        return None
    try:
        headers = getattr(exc.resp, "headers", None) or {}
        value = headers.get("retry-after") or headers.get("Retry-After")
        if not value:
            return None
        # Retry-After may be a number of seconds or an HTTP date.
        try:
            return float(value)
        except (TypeError, ValueError):
            dt = parsedate_to_datetime(value)
            delta = dt.timestamp() - time.time()
            return max(0.0, delta)
    except Exception:
        return None


def _backoff_delay(attempt: int, exc: Optional[HttpError] = None) -> float:
    """Exponential backoff with jitter, honouring Retry-After when present."""
    exp = BACKOFF_BASE * (2 ** (attempt - 1))
    delay = min(BACKOFF_MAX, exp)
    # Add up to 25% jitter to avoid thundering-herd retries.
    delay += random.uniform(0, delay * 0.25)
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(BACKOFF_MAX, delay)


def _notify(on_retry: Optional[RetryCallback], message: str) -> None:
    if on_retry is not None:
        try:
            on_retry(message)
        except Exception:
            logger.warning(message)
    else:
        logger.warning(message)


# ---------------------------------------------------------------------------
# Request execution
# ---------------------------------------------------------------------------


def api_error_from_http(action: str, exc: HttpError) -> YouTubeAPIError:
    """Convert a raw ``HttpError`` into a structured YouTubeAPIError."""
    status = getattr(exc.resp, "status", None)
    reason, detail = _parse_error(exc)
    return YouTubeAPIError(
        action,
        status=status,
        reason=reason,
        detail=detail,
        retryable=_is_retryable(status, reason),
    )


def execute_request(
    request,
    action: str,
    *,
    limiter: Optional[RateLimiter] = None,
    max_retries: int = MAX_RETRIES,
    on_retry: Optional[RetryCallback] = None,
    on_attempt: Optional[Callable[[], None]] = None,
):
    """Execute a ``googleapiclient`` request with throttling + retries.

    Args:
        request: A ``googleapiclient`` HttpRequest (anything with ``execute``).
        action: Human-readable description used in error / retry messages.
        limiter: Optional :class:`RateLimiter` shared across requests.
        max_retries: Maximum number of retry attempts for transient failures.
        on_retry: Optional callback invoked with a message before each retry.
        on_attempt: Optional callback invoked immediately before each HTTP
            attempt (including retries). It may raise to abort the request —
            used here to enforce the local daily-quota budget.

    Returns:
        The decoded JSON response from the API.

    Raises:
        YouTubeAPIError: When the request fails and cannot be retried.
    """
    if limiter is not None:
        limiter.wait()

    attempt = 0
    while True:
        if on_attempt is not None:
            on_attempt()
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            reason, detail = _parse_error(exc)
            retryable = _is_retryable(status, reason)

            if retryable and attempt < max_retries:
                attempt += 1
                delay = _backoff_delay(attempt, exc)
                msg = (
                    f"{action}: HTTP {status}"
                    + (f" {reason}" if reason else "")
                    + f" — retry {attempt}/{max_retries} in {delay:.1f}s"
                )
                _notify(on_retry, msg)
                time.sleep(delay)
                if limiter is not None:
                    limiter.wait()
                continue

            raise YouTubeAPIError(
                action,
                status=status,
                reason=reason,
                detail=detail,
                retryable=retryable,
            ) from exc

        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            # Transient network failures: retry with backoff.
            if attempt < max_retries:
                attempt += 1
                delay = _backoff_delay(attempt)
                msg = (
                    f"{action}: network error ({exc.__class__.__name__}) "
                    f"— retry {attempt}/{max_retries} in {delay:.1f}s"
                )
                _notify(on_retry, msg)
                time.sleep(delay)
                if limiter is not None:
                    limiter.wait()
                continue
            raise YouTubeAPIError(
                action,
                reason="networkError",
                detail=str(exc),
                retryable=True,
            ) from exc