"""Exception types for ytmgr."""

from __future__ import annotations

from typing import Optional


class YouTubeAPIError(Exception):
    """Raised when a YouTube Data API call fails.

    Carries structured information about the failure so callers can present a
    clear message (and decide whether a retry could help).
    """

    def __init__(
        self,
        action: str,
        status: Optional[int] = None,
        reason: Optional[str] = None,
        detail: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        self.action = action
        self.status = status
        self.reason = reason
        self.detail = detail
        self.retryable = retryable

        parts = [f"{action} failed"]
        if status is not None:
            parts.append(f"HTTP {status}")
        if reason:
            parts.append(reason)
        message = ": ".join(parts)
        if detail:
            message += f" — {detail}"
        super().__init__(message)

    def friendly(self) -> str:
        """Return a user-facing, actionable message."""
        if self.status == 403 and self.reason in (
            "quotaExceeded",
            "dailyLimitExceeded",
        ):
            return (
                f"{self.action} failed: YouTube API quota exceeded for the day. "
                "Try again after midnight Pacific Time, or use a different "
                "Google Cloud project / OAuth client."
            )
        if self.status == 403 and self.reason in (
            "rateLimitExceeded",
            "userRateLimitExceeded",
        ):
            return (
                f"{self.action} failed: rate limit exceeded even after retries. "
                "Slow down requests (increase YTMGR_MIN_INTERVAL) or try again later."
            )
        if self.status == 401:
            return (
                f"{self.action} failed: authorization expired or invalid. "
                "Re-run 'ytmgr auth' to sign in again."
            )
        if self.status == 404:
            return f"{self.action} failed: resource not found (HTTP 404)."
        return str(self)
