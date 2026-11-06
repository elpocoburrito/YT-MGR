"""YouTube Data API quota accounting.

The YouTube Data API has a daily quota (default 10,000 units per Google Cloud
project). Different endpoints cost different amounts, and writes are expensive:

===========================  ======
Operation                    Units
===========================  ======
``playlists.list``           1
``playlists.insert``         50
``playlists.delete``         50
``playlistItems.list``       1  (per page of up to 50)
``playlistItems.insert``     50
``playlistItems.delete``     50
===========================  ======

This module tracks usage across runs (persisted to ``~/.ytmgr/quota.json``),
estimates the cost of upcoming operations, and can refuse a request that would
blow through the daily budget — so the tool fails gracefully with a clear
message instead of hammering the API into a ``403 quotaExceeded``.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Optional

from ytmgr.config import (
    DAILY_QUOTA_LIMIT,
    QUOTA_ENFORCE,
    QUOTA_FILE,
    QUOTA_WARN_RATIO,
)
from ytmgr.errors import YouTubeAPIError

# Per-operation quota costs (YouTube Data API).
COST_PLAYLISTS_LIST = 1
COST_PLAYLISTS_INSERT = 50
COST_PLAYLISTS_DELETE = 50
COST_ITEMS_LIST = 1
COST_ITEMS_INSERT = 50
COST_ITEMS_DELETE = 50
COST_VIDEOS_LIST = 1

COST_TABLE = {
    "playlists.list": COST_PLAYLISTS_LIST,
    "playlists.insert": COST_PLAYLISTS_INSERT,
    "playlists.delete": COST_PLAYLISTS_DELETE,
    "playlistItems.list": COST_ITEMS_LIST,
    "playlistItems.insert": COST_ITEMS_INSERT,
    "playlistItems.delete": COST_ITEMS_DELETE,
    "videos.list": COST_VIDEOS_LIST,
}

# Maximum number of playlist items returned per page.
ITEMS_PAGE_SIZE = 50
# Maximum number of playlists returned per page.
PLAYLISTS_PAGE_SIZE = 50


def estimate_list_item_pages(item_count: int) -> int:
    """Number of ``playlistItems.list`` requests to page through ``item_count``."""
    if item_count <= 0:
        return 1
    return (item_count + ITEMS_PAGE_SIZE - 1) // ITEMS_PAGE_SIZE


def estimate_list_items_units(item_count: int) -> int:
    """Quota units to list ``item_count`` items (1 unit per page)."""
    return estimate_list_item_pages(item_count) * COST_ITEMS_LIST


def estimate_insert_units(count: int) -> int:
    """Quota units to insert ``count`` items."""
    return max(0, count) * COST_ITEMS_INSERT


def estimate_delete_units(count: int) -> int:
    """Quota units to delete ``count`` items."""
    return max(0, count) * COST_ITEMS_DELETE


class QuotaExceeded(YouTubeAPIError):
    """Raised locally before a request that would exceed the daily quota."""

    def __init__(
        self,
        action: str,
        needed: int,
        remaining: int,
        limit: int,
    ) -> None:
        self.needed = needed
        self.remaining = remaining
        self.limit = limit
        super().__init__(
            action,
            reason="localQuotaGuard",
            detail=f"needs {needed} units, {remaining} of {limit} remaining",
            retryable=False,
        )

    def friendly(self) -> str:
        return (
            f"{self.action} stopped: it needs {self.needed} quota units but only "
            f"{self.remaining} of {self.limit} remain today. The daily quota "
            "resets at midnight Pacific Time. Progress has been saved where "
            "applicable — re-run the same command later to continue."
        )


class QuotaTracker:
    """Thread-safe, per-day quota tracker persisted to disk."""

    def __init__(
        self,
        limit: int = DAILY_QUOTA_LIMIT,
        path: Path = QUOTA_FILE,
        enforce: bool = QUOTA_ENFORCE,
        warn_ratio: float = QUOTA_WARN_RATIO,
    ) -> None:
        self.limit = max(1, int(limit))
        self.path = Path(path)
        self.enforce = enforce
        self.warn_ratio = min(max(warn_ratio, 0.0), 1.0)
        self._lock = threading.Lock()
        self._day = date.today().isoformat()
        self._used = 0
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("date") == self._day:
                self._used = max(0, int(data.get("used", 0)))
        except Exception:
            self._used = 0

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"date": self._day, "used": self._used}),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            # Quota persistence is best-effort; never fail the operation.
            pass

    def _maybe_reset(self) -> None:
        today = date.today().isoformat()
        if today != self._day:
            self._day = today
            self._used = 0

    # -- public API ----------------------------------------------------------

    @property
    def used(self) -> int:
        with self._lock:
            self._maybe_reset()
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            self._maybe_reset()
            return max(0, self.limit - self._used)

    @property
    def day(self) -> str:
        with self._lock:
            self._maybe_reset()
            return self._day

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_reset()
            return {
                "date": self._day,
                "used": self._used,
                "limit": self.limit,
                "remaining": max(0, self.limit - self._used),
                "percent": (self._used / self.limit) if self.limit else 0.0,
            }

    def consume(self, units: int, action: str = "request") -> Optional[str]:
        """Reserve ``units`` of quota.

        Raises:
            QuotaExceeded: if enforcement is on and the request would exceed the
                daily budget.

        Returns:
            A warning message when usage just crossed the warning threshold,
            otherwise ``None``.
        """
        units = max(0, int(units))
        with self._lock:
            self._maybe_reset()
            if self.enforce and (self._used + units) > self.limit:
                raise QuotaExceeded(
                    action,
                    needed=units,
                    remaining=max(0, self.limit - self._used),
                    limit=self.limit,
                )

            before = self._used
            self._used += units
            self._save()

            threshold = int(self.limit * self.warn_ratio)
            crossed = before < threshold <= self._used
            if crossed:
                return (
                    f"Quota warning: {self._used}/{self.limit} units used today "
                    f"({self._used / self.limit:.0%}). "
                    f"{max(0, self.limit - self._used)} units remaining."
                )
            return None

    def refund(self, units: int) -> None:
        """Return previously-reserved units (used when a batch is retried)."""
        units = max(0, int(units))
        if not units:
            return
        with self._lock:
            self._maybe_reset()
            self._used = max(0, self._used - units)
            self._save()

    def add_usage(self, units: int) -> None:
        """Record usage *without* enforcing the limit.

        Used by the quota probe, which must reach the server even when the local
        estimate believes the budget is exhausted. Usage is capped at the limit
        so the display never exceeds 100%.
        """
        units = max(0, int(units))
        if not units:
            return
        with self._lock:
            self._maybe_reset()
            self._used = min(self.limit, self._used + units)
            self._save()

    def mark_exhausted(self) -> None:
        """Mark the daily budget as exhausted (after a real 403 quotaExceeded)."""
        with self._lock:
            self._maybe_reset()
            self._used = self.limit
            self._save()
