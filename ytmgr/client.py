"""High-level wrapper around the YouTube Data API v3 playlist endpoints.

Design goals
------------
* **Minimise quota usage**: every call is tagged with its quota cost and tracked
  against a persisted daily budget. Redundant reads are cached, and expensive
  bulk operations are estimated and guarded before they run.
* **Minimise hammering**: requests are throttled, retried with backoff, and bulk
  inserts are sent as *batched* HTTP requests (up to 50 sub-requests each),
  which drastically reduces the number of round trips for large playlists.
* **Scale to 3000+ video playlists**: listing is fully paginated (50/page),
  cloning is incremental and *resumable* across days via an on-disk checkpoint,
  and duplicate source videos are skipped by default.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from googleapiclient.discovery import Resource

from ytmgr.config import (
    BATCH_SIZE,
    CLONE_DIR,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL,
    USE_BATCH,
)
from ytmgr.errors import YouTubeAPIError
from ytmgr.quota import (
    COST_ITEMS_DELETE,
    COST_ITEMS_INSERT,
    COST_ITEMS_LIST,
    COST_PLAYLISTS_DELETE,
    COST_PLAYLISTS_INSERT,
    COST_PLAYLISTS_LIST,
    ITEMS_PAGE_SIZE,
    PLAYLISTS_PAGE_SIZE,
    QuotaExceeded,
    QuotaTracker,
)
from ytmgr.ratelimit import RateLimiter, RetryCallback, execute_request

logger = logging.getLogger("ytmgr.api")


# ---------------------------------------------------------------------------
# Progress callback type
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[str, int, Optional[int]], None]
"""
Signature: (message, current, total) -> None

Called during long-running operations to report progress.
``total`` may be None when the total is unknown (e.g. during pagination).
"""

ResultCallback = Callable[[int, str, bool, Optional[BaseException]], None]
"""Signature: (index, video_id, success, error) -> None (used for batching)."""

QuotaWarnCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/|^)([a-zA-Z0-9_-]{11})"
)


def extract_video_id(url_or_id: str) -> str:
    """Extract a YouTube video ID from a URL or return the input if it is one."""
    if re.match(r"^[a-zA-Z0-9_-]{11}$", url_or_id):
        return url_or_id

    m = _VIDEO_ID_RE.search(url_or_id)
    if m:
        return m.group(1)

    raise ValueError(f"Could not extract a YouTube video ID from: {url_or_id}")


def _err_text(exc: Optional[BaseException]) -> str:
    if isinstance(exc, YouTubeAPIError):
        return exc.friendly()
    return str(exc) if exc is not None else "unknown error"


def _item_video_id(item: dict) -> str:
    return (
        item.get("snippet", {})
        .get("resourceId", {})
        .get("videoId", "")
    )


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return (slug[:max_len] or "clone")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class YouTubeClient:
    """Wraps YouTube Data API v3 playlist + playlistItems endpoints."""

    def __init__(
        self,
        service: Resource,
        *,
        min_interval: Optional[float] = None,
        max_retries: int = MAX_RETRIES,
        on_retry: Optional[RetryCallback] = None,
        on_quota_warning: Optional[QuotaWarnCallback] = None,
        quota: Optional[QuotaTracker] = None,
        use_batch: Optional[bool] = None,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.service = service
        self.playlists_api = service.playlists()
        self.items_api = service.playlistItems()
        self.limiter = RateLimiter(
            MIN_REQUEST_INTERVAL if min_interval is None else min_interval
        )
        self.max_retries = max_retries
        self.on_retry = on_retry
        self.on_quota_warning = on_quota_warning
        self.quota = quota if quota is not None else QuotaTracker()
        self.use_batch = USE_BATCH if use_batch is None else use_batch
        self.batch_size = max(1, min(50, batch_size))

        # In-memory caches (invalidated after any mutation).
        self._items_cache: dict[str, list[dict]] = {}
        self._playlists_cache: Optional[list[dict]] = None

    # -- Request plumbing ----------------------------------------------------

    def _emit_quota_warning(self, message: str) -> None:
        if self.on_quota_warning is not None:
            try:
                self.on_quota_warning(message)
                return
            except Exception:
                pass
        logger.warning(message)

    def _execute(self, request, action: str, cost: int = 0):
        """Execute an API request with throttling, quota accounting, retries."""

        def _on_attempt() -> None:
            if cost:
                warning = self.quota.consume(cost, action=action)
                if warning:
                    self._emit_quota_warning(warning)

        return execute_request(
            request,
            action,
            limiter=self.limiter,
            max_retries=self.max_retries,
            on_retry=self.on_retry,
            on_attempt=_on_attempt if cost else None,
        )

    def _invalidate(self) -> None:
        """Drop cached read results after a mutation."""
        self._items_cache.clear()
        self._playlists_cache = None

    # -- Quota verification --------------------------------------------------

    def probe_quota(self) -> dict:
        """Verify quota availability against the live YouTube API.

        The YouTube Data API has no endpoint that returns your absolute
        remaining quota, so this performs the cheapest possible real call (one
        ``playlists.list``, costing 1 unit) and interprets the result:

        * ``ok=True``  — the API answered, so the quota is *not* exhausted.
        * ``exhausted=True`` — the API returned 403 ``quotaExceeded`` /
          ``dailyLimitExceeded``; the local tracker is marked exhausted.
        * ``ok=False`` — any other failure (auth, network, rate limit, …).

        The probe bypasses the *local* quota guard so it can still reach the
        server when the local estimate says the budget is spent.

        Returns:
            A dict with ``ok``, ``exhausted``, ``status``, ``reason`` and
            optionally ``error`` (friendly message).
        """
        request = self.playlists_api.list(
            part="snippet", mine=True, maxResults=1
        )
        try:
            # No on_attempt hook => no local enforcement; still throttled+retried.
            execute_request(
                request,
                "quota probe",
                limiter=self.limiter,
                max_retries=self.max_retries,
                on_retry=self.on_retry,
            )
        except YouTubeAPIError as exc:
            if exc.status == 403 and exc.reason in (
                "quotaExceeded",
                "dailyLimitExceeded",
            ):
                self.quota.mark_exhausted()
                return {
                    "ok": False,
                    "exhausted": True,
                    "status": exc.status,
                    "reason": exc.reason,
                    "error": exc.friendly(),
                }
            return {
                "ok": False,
                "exhausted": False,
                "status": exc.status,
                "reason": exc.reason,
                "error": exc.friendly(),
            }

        # The request succeeded, so at least one unit was available.
        self.quota.add_usage(COST_PLAYLISTS_LIST)
        return {
            "ok": True,
            "exhausted": False,
            "status": 200,
            "reason": None,
        }

    # -- Playlists -----------------------------------------------------------

    def list_playlists(
        self, mine: bool = True, page_token: Optional[str] = None
    ) -> dict:
        """List one page of the authenticated user's playlists."""
        kwargs: dict = {
            "part": "snippet,contentDetails",
            "mine": mine,
            "maxResults": PLAYLISTS_PAGE_SIZE,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        return self._execute(
            self.playlists_api.list(**kwargs),
            "list playlists",
            COST_PLAYLISTS_LIST,
        )

    def list_all_playlists(
        self,
        mine: bool = True,
        progress_cb: Optional[ProgressCallback] = None,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> list[dict]:
        """Paginate through *all* of the user's playlists."""
        if use_cache and not refresh and self._playlists_cache is not None:
            return list(self._playlists_cache)

        playlists: list[dict] = []
        page_token: Optional[str] = None
        page = 0
        while True:
            page += 1
            resp = self.list_playlists(mine=mine, page_token=page_token)
            batch = resp.get("items", [])
            playlists.extend(batch)
            if progress_cb:
                progress_cb(
                    f"Fetched playlist page {page} ({len(playlists)} total)",
                    len(playlists),
                    None,
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if use_cache:
            self._playlists_cache = list(playlists)
        return playlists

    def get_playlist(self, playlist_id: str) -> dict:
        """Fetch a single playlist by id (snippet + contentDetails)."""
        resp = self._execute(
            self.playlists_api.list(
                part="snippet,contentDetails", id=playlist_id
            ),
            f"get playlist {playlist_id}",
            COST_PLAYLISTS_LIST,
        )
        return resp.get("items", [{}])[0]

    def create_playlist(
        self,
        title: str,
        description: str = "",
        privacy_status: str = "private",
    ) -> dict:
        """Create a new playlist."""
        body = {
            "snippet": {
                "title": title,
                "description": description,
            },
            "status": {"privacyStatus": privacy_status},
        }
        result = self._execute(
            self.playlists_api.insert(part="snippet,status", body=body),
            f"create playlist '{title}'",
            COST_PLAYLISTS_INSERT,
        )
        self._invalidate()
        return result

    def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist and all its items."""
        self._execute(
            self.playlists_api.delete(id=playlist_id),
            f"delete playlist {playlist_id}",
            COST_PLAYLISTS_DELETE,
        )
        self._invalidate()

    # -- Playlist Items ------------------------------------------------------

    def list_items(
        self,
        playlist_id: str,
        part: str = "snippet",
        page_token: Optional[str] = None,
    ) -> dict:
        """List one page (up to 50) of items in a playlist."""
        kwargs: dict = {
            "part": part,
            "playlistId": playlist_id,
            "maxResults": ITEMS_PAGE_SIZE,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        return self._execute(
            self.items_api.list(**kwargs),
            f"list playlist items {playlist_id}",
            COST_ITEMS_LIST,
        )

    def iter_items(
        self,
        playlist_id: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Iterator[dict]:
        """Yield items page by page without holding the whole playlist."""
        page_token: Optional[str] = None
        page = 0
        seen = 0
        while True:
            page += 1
            resp = self.list_items(playlist_id, page_token=page_token)
            batch = resp.get("items", [])
            seen += len(batch)
            if progress_cb:
                progress_cb(
                    f"Fetched page {page} ({seen} items so far)", seen, None
                )
            yield from batch
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def list_all_items(
        self,
        playlist_id: str,
        progress_cb: Optional[ProgressCallback] = None,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> list[dict]:
        """Paginate through *all* items in a playlist (50 per request)."""
        if use_cache and not refresh and playlist_id in self._items_cache:
            cached = self._items_cache[playlist_id]
            if progress_cb:
                progress_cb(
                    f"Using cached items ({len(cached)} items)", len(cached), len(cached)
                )
            return list(cached)

        items: list[dict] = []
        page = 0
        page_token: Optional[str] = None
        if progress_cb:
            progress_cb("Fetching playlist items…", 0, None)

        while True:
            page += 1
            resp = self.list_items(playlist_id, page_token=page_token)
            batch = resp.get("items", [])
            items.extend(batch)
            if progress_cb:
                progress_cb(
                    f"Fetched page {page} ({len(batch)} items, {len(items)} total)",
                    len(items),
                    None,
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if use_cache:
            self._items_cache[playlist_id] = list(items)
        if progress_cb:
            progress_cb(f"Done — {len(items)} item(s) loaded", len(items), len(items))
        return items

    def add_item(self, playlist_id: str, video_id: str) -> dict:
        """Add a single video to a playlist."""
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        result = self._execute(
            self.items_api.insert(part="snippet", body=body),
            f"add video {video_id} to playlist {playlist_id}",
            COST_ITEMS_INSERT,
        )
        self._invalidate()
        return result

    def add_items(
        self,
        playlist_id: str,
        video_ids: list[str],
        progress_cb: Optional[ProgressCallback] = None,
        use_batch: Optional[bool] = None,
        on_result: Optional[ResultCallback] = None,
    ) -> list[tuple[str, bool, Optional[BaseException]]]:
        """Add many videos, using batched HTTP requests when possible.

        Returns a list of ``(video_id, success, error)`` tuples in input order.
        """
        if not video_ids:
            return []

        if use_batch is None:
            use_batch = self.use_batch

        total = len(video_ids)
        results: list[tuple[str, bool, Optional[BaseException]]] = []

        can_batch = (
            use_batch
            and total > 1
            and hasattr(self.service, "new_batch_http_request")
        )

        try:
            if can_batch:
                for start in range(0, total, self.batch_size):
                    chunk = video_ids[start:start + self.batch_size]
                    chunk_results = self._add_chunk_batched(playlist_id, chunk)
                    for local, (vid, ok, err) in enumerate(chunk_results):
                        idx = start + local
                        results.append((vid, ok, err))
                        self._report_add(progress_cb, on_result, idx, vid, ok, err, total)
            else:
                for idx, vid in enumerate(video_ids):
                    try:
                        self.add_item(playlist_id, vid)
                        results.append((vid, True, None))
                        self._report_add(progress_cb, on_result, idx, vid, True, None, total)
                    except QuotaExceeded:
                        raise
                    except Exception as exc:  # noqa: BLE001 - report per-item failure
                        results.append((vid, False, exc))
                        self._report_add(progress_cb, on_result, idx, vid, False, exc, total)
        finally:
            # Some items may have been added before an interruption/error.
            self._invalidate()

        return results

    @staticmethod
    def _report_add(progress_cb, on_result, idx, vid, ok, err, total) -> None:
        if on_result:
            on_result(idx, vid, ok, err)
        if progress_cb:
            if ok:
                progress_cb(f"[{idx+1}/{total}] Added {vid}", idx + 1, total)
            else:
                progress_cb(
                    f"[{idx+1}/{total}] Failed {vid}: {_err_text(err)}",
                    idx + 1,
                    total,
                )

    def _add_chunk_batched(
        self, playlist_id: str, chunk: list[str]
    ) -> list[tuple[str, bool, Optional[BaseException]]]:
        """Insert a chunk of videos in a single batched HTTP request."""
        responses: dict[str, tuple[object, Optional[BaseException]]] = {}

        def _callback(request_id, response, exception) -> None:
            responses[request_id] = (response, exception)

        cost = COST_ITEMS_INSERT * len(chunk)
        # Consume quota up-front for the whole chunk (may raise QuotaExceeded).
        warning = self.quota.consume(
            cost, action=f"batch add {len(chunk)} videos to {playlist_id}"
        )
        if warning:
            self._emit_quota_warning(warning)

        batch = self.service.new_batch_http_request(callback=_callback)
        for i, vid in enumerate(chunk):
            body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": vid},
                }
            }
            batch.add(self.items_api.insert(part="snippet", body=body), request_id=str(i))

        try:
            self.limiter.wait()
            batch.execute()
        except Exception:  # noqa: BLE001 - batch endpoints can be unavailable
            # Refund the pre-charged quota and fall back to individual requests,
            # which have their own retry/backoff handling.
            self.quota.refund(cost)
            return [self._add_one_sequential(playlist_id, vid) for vid in chunk]

        results: list[tuple[str, bool, Optional[BaseException]]] = []
        for i, vid in enumerate(chunk):
            _resp, exc = responses.get(
                str(i), (None, RuntimeError("no response in batch"))
            )
            results.append((vid, exc is None, exc))
        return results

    def _add_one_sequential(
        self, playlist_id: str, video_id: str
    ) -> tuple[str, bool, Optional[BaseException]]:
        try:
            self.add_item(playlist_id, video_id)
            return (video_id, True, None)
        except QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            return (video_id, False, exc)

    def clone_playlist(
        self,
        source_playlist_id: str,
        new_title: str,
        description: str = "",
        privacy_status: str = "private",
        progress_cb: Optional[ProgressCallback] = None,
        *,
        resume: bool = True,
        skip_duplicates: bool = True,
        limit: Optional[int] = None,
        use_batch: Optional[bool] = None,
    ) -> dict:
        """Clone a playlist, optionally resuming across days.

        The clone reads the source playlist once, then copies unique videos into
        a new playlist. A checkpoint is written to ``~/.ytmgr/clones`` after each
        successful insert so an interrupted or quota-limited clone can be resumed
        by running the same command again (with the same source and title).

        Args:
            resume: Reuse/continue an existing checkpoint when present.
            skip_duplicates: Copy each video only once (saves 50 units per dup).
            limit: Only add at most this many videos in this run.
            use_batch: Override batching for this clone.
        """
        # 1. Read the source (one listing, fully paginated, cached).
        if progress_cb:
            progress_cb("Reading source playlist…", 0, None)
        source_items = self.list_all_items(source_playlist_id, progress_cb=progress_cb)
        if not source_items:
            raise RuntimeError("Source playlist is empty — nothing to clone.")

        # 2. Build an ordered list of source video IDs.
        video_ids: list[str] = []
        seen: set[str] = set()
        for item in source_items:
            vid = _item_video_id(item)
            if not vid:
                continue
            if skip_duplicates:
                if vid in seen:
                    continue
                seen.add(vid)
            video_ids.append(vid)

        total_source = len(video_ids)
        if total_source == 0:
            raise RuntimeError("Source playlist has no video IDs — nothing to clone.")

        # 3. Load or create the checkpoint / target playlist.
        cp_path = self._checkpoint_path(source_playlist_id, new_title)
        checkpoint = self._load_checkpoint(cp_path) if resume else None

        added_indices: list[int] = []
        target_id: Optional[str] = None

        if checkpoint and checkpoint.get("source_id") == source_playlist_id:
            target_id = checkpoint.get("target_id")
            added_indices = list(checkpoint.get("added_indices", []))
            if progress_cb:
                progress_cb(
                    f"Resuming into {target_id} "
                    f"({len(added_indices)}/{total_source} already added)",
                    len(added_indices),
                    total_source,
                )
        else:
            if progress_cb:
                progress_cb("Creating new playlist…", 0, total_source)
            new_playlist = self.create_playlist(
                title=new_title,
                description=description,
                privacy_status=privacy_status,
            )
            target_id = new_playlist.get("id", "")
            if not target_id:
                raise RuntimeError(
                    f"Failed to create playlist '{new_title}' — no id returned."
                )
            checkpoint = {
                "source_id": source_playlist_id,
                "target_id": target_id,
                "new_title": new_title,
                "added_indices": added_indices,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_checkpoint(cp_path, checkpoint)

        assert target_id is not None
        added_set = set(added_indices)
        pending_indices = [i for i in range(total_source) if i not in added_set]
        if limit is not None:
            pending_indices = pending_indices[: max(0, limit)]

        # 4. Nothing to do? Finalise.
        if not pending_indices:
            complete = len(added_set) >= total_source
            if complete:
                self._delete_checkpoint(cp_path)
            return self._clone_result(
                target_id, new_title, added_run=0, already=len(added_set),
                errors=0, total_source=total_source, complete=complete,
                quota_exhausted=False, checkpoint=None if complete else str(cp_path),
            )

        pending_videos = [video_ids[i] for i in pending_indices]

        added_run = 0
        errors = 0
        quota_exhausted = False

        def _on_result(local_idx: int, vid: str, ok: bool, err) -> None:
            nonlocal added_run, errors
            global_idx = pending_indices[local_idx]
            if ok:
                added_indices.append(global_idx)
                added_set.add(global_idx)
                checkpoint["added_indices"] = added_indices
                self._save_checkpoint(cp_path, checkpoint)
                added_run += 1
            else:
                errors += 1
            if progress_cb:
                if ok:
                    progress_cb(
                        f"[{len(added_set)}/{total_source}] Added {vid}",
                        len(added_set),
                        total_source,
                    )
                else:
                    progress_cb(
                        f"[{len(added_set)}/{total_source}] Failed {vid}: {_err_text(err)}",
                        len(added_set),
                        total_source,
                    )

        try:
            self.add_items(
                target_id,
                pending_videos,
                progress_cb=None,
                use_batch=use_batch,
                on_result=_on_result,
            )
        except QuotaExceeded as exc:
            quota_exhausted = True
            if progress_cb:
                progress_cb(exc.friendly(), len(added_set), total_source)

        complete = len(added_set) >= total_source
        if complete:
            self._delete_checkpoint(cp_path)

        return self._clone_result(
            target_id,
            new_title,
            added_run=added_run,
            already=len(added_set) - added_run,
            errors=errors,
            total_source=total_source,
            complete=complete,
            quota_exhausted=quota_exhausted,
            checkpoint=None if complete else str(cp_path),
        )

    @staticmethod
    def _clone_result(
        target_id: str,
        new_title: str,
        *,
        added_run: int,
        already: int,
        errors: int,
        total_source: int,
        complete: bool,
        quota_exhausted: bool,
        checkpoint: Optional[str],
    ) -> dict:
        return {
            "id": target_id,
            "snippet": {"title": new_title},
            "_clone_stats": {
                "added": added_run,
                "already_added": already,
                "errors": errors,
                "total_source": total_source,
                "complete": complete,
                "quota_exhausted": quota_exhausted,
                "checkpoint": checkpoint,
            },
        }

    # -- Clone checkpoints ---------------------------------------------------

    def _checkpoint_path(self, source_playlist_id: str, new_title: str) -> Path:
        return CLONE_DIR / f"{source_playlist_id}__{_slugify(new_title)}.json"

    @staticmethod
    def _load_checkpoint(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _save_checkpoint(path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    @staticmethod
    def _delete_checkpoint(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # -- Deletion ------------------------------------------------------------

    def delete_item(self, item_id: str) -> None:
        """Remove a single item (video entry) from a playlist."""
        self._execute(
            self.items_api.delete(id=item_id),
            f"delete playlist item {item_id}",
            COST_ITEMS_DELETE,
        )
        self._invalidate()

    def delete_items(
        self,
        item_ids: list[str],
        progress_cb: Optional[ProgressCallback] = None,
    ) -> list[tuple[str, bool]]:
        """Remove multiple items from a playlist."""
        total = len(item_ids)
        results: list[tuple[str, bool]] = []
        for idx, item_id in enumerate(item_ids, start=1):
            try:
                self.delete_item(item_id)
                results.append((item_id, True))
                if progress_cb:
                    progress_cb(f"[{idx}/{total}] Deleted {item_id}", idx, total)
            except QuotaExceeded:
                raise
            except YouTubeAPIError as exc:
                results.append((item_id, False))
                if progress_cb:
                    progress_cb(
                        f"[{idx}/{total}] Failed {item_id}: {exc.friendly()}", idx, total
                    )
            except Exception as exc:  # noqa: BLE001
                results.append((item_id, False))
                if progress_cb:
                    progress_cb(
                        f"[{idx}/{total}] Failed {item_id}: {exc}", idx, total
                    )
        return results

    # -- Duplicate handling --------------------------------------------------

    def find_duplicate_items(
        self,
        playlist_id: str,
        items: Optional[list[dict]] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> list[list[dict]]:
        """Return groups of items that share the same videoId.

        Groups preserve playlist order (oldest first); ``group[0]`` is the one to
        keep. Pass ``items`` to reuse an already-fetched listing and avoid an
        extra API round trip.
        """
        if items is None:
            items = self.list_all_items(playlist_id, progress_cb=progress_cb)

        seen: dict[str, list[dict]] = {}
        for item in items:
            vid = _item_video_id(item)
            if vid:
                seen.setdefault(vid, []).append(item)

        return [group for group in seen.values() if len(group) > 1]

    def remove_duplicates(
        self,
        playlist_id: str,
        progress_cb: Optional[ProgressCallback] = None,
        groups: Optional[list[list[dict]]] = None,
    ) -> list[str]:
        """Remove duplicate videos, keeping the oldest occurrence.

        Pass ``groups`` (from :meth:`find_duplicate_items`) to avoid re-listing
        the whole playlist — saving ``ceil(n/50)`` quota units and requests.
        """
        if groups is None:
            groups = self.find_duplicate_items(playlist_id)

        total_dups = sum(len(g) - 1 for g in groups)
        if progress_cb:
            progress_cb(
                f"Found {len(groups)} video(s) with duplicates "
                f"({total_dups} to remove)",
                0,
                total_dups,
            )

        deleted: list[str] = []
        done = 0
        for group in groups:
            for dup in group[1:]:
                dup_id = dup.get("id", "")
                if not dup_id:
                    continue
                try:
                    self.delete_item(dup_id)
                    deleted.append(dup_id)
                    done += 1
                    if progress_cb:
                        progress_cb(
                            f"Removed duplicate {done}/{total_dups}",
                            done,
                            total_dups,
                        )
                except QuotaExceeded:
                    raise
                except YouTubeAPIError as exc:
                    done += 1
                    if progress_cb:
                        progress_cb(
                            f"Failed to remove {dup_id}: {exc.friendly()}",
                            done,
                            total_dups,
                        )
                except Exception as exc:  # noqa: BLE001
                    done += 1
                    if progress_cb:
                        progress_cb(
                            f"Failed to remove {dup_id}: {exc}", done, total_dups
                        )

        if progress_cb:
            progress_cb(
                f"Done — {len(deleted)} duplicate(s) deleted",
                len(deleted),
                total_dups,
            )
        return deleted

