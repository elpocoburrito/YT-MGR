"""Command-line interface for the YouTube Playlist Manager."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from ytmgr.auth import authenticate, get_authenticated_service, revoke_token
from ytmgr.client import YouTubeClient, extract_video_id
from ytmgr.errors import YouTubeAPIError
from ytmgr.quota import (
    COST_ITEMS_DELETE,
    COST_ITEMS_INSERT,
    COST_ITEMS_LIST,
    COST_PLAYLISTS_DELETE,
    COST_PLAYLISTS_INSERT,
    COST_PLAYLISTS_LIST,
    QuotaTracker,
    estimate_delete_units,
    estimate_insert_units,
    estimate_list_items_units,
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_playlist(p: dict) -> str:
    s = p.get("snippet", {})
    title = s.get("title", "(untitled)")
    pid = p.get("id", "?")
    pad = " " * 2
    items = p.get("contentDetails", {}).get("itemCount", "?")
    return f"  {pad}{title}  [{pid}]  ({items} videos)"


def _fmt_item(item: dict, idx: int = 0) -> str:
    s = item.get("snippet", {})
    title = s.get("title", "(unknown)")
    vid = s.get("resourceId", {}).get("videoId", "?")
    pos = s.get("position", idx)
    iid = item.get("id", "?")
    pad = " " * 2
    return f"  {pad}#{pos:>3d}  {title[:70]:70s}  {vid}  [{iid}]"


def _print_quota(client, units: Optional[int] = None, label: str = "") -> bool:
    """Show quota status/cost. Return True if the budget covers the estimate."""
    try:
        snap = client.quota.snapshot()
    except Exception:
        return True
    used = snap["used"]
    limit = snap["limit"]
    remaining = snap["remaining"]
    print(
        f"  Quota: {used}/{limit} units used today "
        f"({snap['percent']:.0%}), {remaining} remaining."
    )
    if units is not None:
        print(f"  Estimated cost{(' for ' + label) if label else ''}: {units} units.")
        if units > remaining:
            print(
                "  WARNING: this exceeds today's remaining quota; the operation will "
                "stop early (clone progress is checkpointed and resumable)."
            )
            return False
    return True


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(prompt).strip().lower() in ("y", "yes")


def _fmt_quota(snap: dict) -> str:
    bar_len = 30
    pct = snap["percent"]
    filled = int(bar_len * pct)
    bar = "#" * filled + "-" * (bar_len - filled)
    return (
        f"  Date:      {snap['date']}\n"
        f"  Used:      {snap['used']} / {snap['limit']} units ({pct:.1%})\n"
        f"  Remaining: {snap['remaining']} units\n"
        f"  [{bar}]"
    )


def _fmt_group(group: list[dict], idx: int) -> str:
    lines: list[str] = [
        f"\n  [{idx}] Video appears {len(group)} times:"
    ]
    for i, item in enumerate(group):
        vid = item.get("snippet", {}).get("resourceId", {}).get("videoId", "?")
        title = item.get("snippet", {}).get("title", "?")
        iid = item.get("id", "?")
        marker = "KEEP" if i == 0 else "DEL "
        lines.append(f"    {marker}  #{i}  {title[:60]:60s}  {vid}  [{iid}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_auth(client, args) -> int:
    """Force re-authentication and save a new token, or revoke an existing one."""
    if args.revoke:
        revoke_token()
        return 0
    authenticate(force=True)
    print("Authentication successful. Token saved.")
    return 0


def cmd_create_playlist(client, args) -> int:
    p = client.create_playlist(
        title=args.title,
        description=args.description,
        privacy_status=args.privacy,
    )
    pid = p.get("id", "?")
    print(f"Created playlist: {args.title}  [{pid}]")
    return 0


def cmd_delete_playlist(client, args) -> int:
    confirm = input(
        f"Are you sure you want to delete playlist '{args.playlist_id}'? "
        f"This cannot be undone. [y/N] "
    )
    if confirm.lower() not in ("y", "yes"):
        print("Aborted.")
        return 1

    client.delete_playlist(args.playlist_id)
    print(f"Deleted playlist: {args.playlist_id}")
    return 0


def cmd_list_playlists(client, args) -> int:
    def _progress(msg, cur, total):
        print(f"  {msg}")

    items = client.list_all_playlists(progress_cb=_progress)
    if not items:
        print("No playlists found.")
        return 0

    print(f"\nFound {len(items)} playlist(s):\n")
    for p in items:
        print(_fmt_playlist(p))
    print()
    return 0


def cmd_list_items(client, args) -> int:
    def _progress(msg, cur, total):
        print(f"  {msg}")

    items = client.list_all_items(args.playlist_id, progress_cb=_progress)
    if not items:
        print("No videos in this playlist.")
        return 0

    print(f"\n{len(items)} video(s) in playlist '{args.playlist_id}':\n")
    for idx, item in enumerate(items):
        print(_fmt_item(item, idx))
    print()
    return 0


def cmd_add(client, args) -> int:
    if not args.video_ids:
        print("No video IDs or URLs provided.")
        return 1

    video_ids: list[str] = []
    invalid = 0
    for raw in args.video_ids:
        try:
            video_ids.append(extract_video_id(raw))
        except ValueError as e:
            print(f"  Skipping {raw}: {e}", file=sys.stderr)
            invalid += 1

    if not video_ids:
        print("No valid video IDs or URLs provided.")
        return 1

    units = estimate_insert_units(len(video_ids))
    fits = _print_quota(client, units, label=f"{len(video_ids)} insert(s)")
    if not fits and not _confirm("Continue anyway? [y/N] ", args.yes):
        print("Aborted.")
        return 1

    def _progress(msg, cur, total):
        print(f"  {msg}")

    results = client.add_items(args.playlist_id, video_ids, progress_cb=_progress)
    ok = sum(1 for _, success, _ in results if success)
    fail = sum(1 for _, success, _ in results if not success) + invalid
    print(f"\nDone. {ok} added, {fail} failed.")
    return 0 if fail == 0 else 1


def cmd_delete_items(client, args) -> int:
    if not args.item_ids:
        print("No item IDs provided.")
        return 1

    units = estimate_delete_units(len(args.item_ids))
    fits = _print_quota(client, units, label=f"{len(args.item_ids)} delete(s)")
    if not fits and not _confirm("Continue anyway? [y/N] ", args.yes):
        print("Aborted.")
        return 1

    def _progress(msg, cur, total):
        print(f"  {msg}")

    results = client.delete_items(args.item_ids, progress_cb=_progress)
    ok = sum(1 for _, s in results if s)
    fail = sum(1 for _, s in results if not s)
    print(f"\nDone. {ok} deleted, {fail} failed.")
    return 0 if fail == 0 else 1


def cmd_tui(client, args) -> int:
    """Launch the interactive TUI."""
    try:
        from ytmgr.tui.app import YtMgrTui
    except ImportError as exc:
        print(f"TUI dependencies not installed: {exc}", file=sys.stderr)
        print("Run: pip install textual", file=sys.stderr)
        return 1
    app = YtMgrTui()
    app.run()
    return 0


def cmd_clone_playlist(client, args) -> int:
    """Clone an existing playlist into a new one (resumable)."""
    snap = client.quota.snapshot()
    capacity = max(0, (snap["remaining"] - COST_PLAYLISTS_INSERT) // COST_ITEMS_INSERT)
    print(
        f"  Quota: {snap['used']}/{snap['limit']} used, "
        f"~{capacity} video insert(s) affordable today."
    )
    print(
        "  Note: cloning costs 50 units per video. Large playlists are cloned "
        "incrementally and can be resumed on later days.\n"
    )

    def _progress(msg, cur, total):
        if total:
            print(f"  [{cur}/{total}] {msg}")
        else:
            print(f"  {msg}")

    result = client.clone_playlist(
        source_playlist_id=args.source_id,
        new_title=args.title,
        description=args.description,
        privacy_status=args.privacy,
        progress_cb=_progress,
        resume=not args.no_resume,
        skip_duplicates=not args.keep_duplicates,
        limit=args.limit,
        use_batch=args.batch,
    )

    new_id = result.get("id", "?")
    stats = result.get("_clone_stats", {})
    print(f"\nClone target: {args.title}  [{new_id}]")
    print(f"  Source videos:   {stats.get('total_source', 0)}")
    print(f"  Added this run:  {stats.get('added', 0)}")
    print(f"  Already present: {stats.get('already_added', 0)}")
    print(f"  Errors:          {stats.get('errors', 0)}")

    if stats.get("complete"):
        print("  DONE: clone complete.")
    else:
        if stats.get("quota_exhausted"):
            print("  PAUSED: daily quota reached.")
        checkpoint = stats.get("checkpoint")
        print("  INCOMPLETE: re-run the same command to resume.")
        if checkpoint:
            print(f"    Checkpoint: {checkpoint}")

    _print_quota(client)
    return 0 if stats.get("errors", 0) == 0 else 1


def cmd_dedup(client, args) -> int:
    groups = client.find_duplicate_items(args.playlist_id)

    if not groups:
        print("No duplicates found.")
        return 0

    print(f"\nFound {len(groups)} video(s) with duplicates:\n")
    for idx, group in enumerate(groups):
        print(_fmt_group(group, idx))

    if not args.yes:
        confirm = input(
            "\nDelete the duplicates shown above (keep the oldest)? [y/N] "
        )
        if confirm.lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    total_dups = sum(len(g) - 1 for g in groups)
    units = estimate_delete_units(total_dups)
    _print_quota(client, units, label=f"{total_dups} delete(s)")

    def _progress(msg, cur, total):
        print(f"  {msg}")

    # Reuse the already-fetched groups so we don't list the playlist twice.
    deleted = client.remove_duplicates(
        args.playlist_id, progress_cb=_progress, groups=groups
    )
    print(f"\nDeleted {len(deleted)} duplicate item(s).")
    return 0


def cmd_quota(client, args) -> int:
    """Show today's API quota usage and the cost of common operations."""
    tracker = client.quota if client is not None else QuotaTracker()
    snap = tracker.snapshot()
    print("\nYouTube Data API quota usage:\n")
    print(_fmt_quota(snap))
    print("\n  Operation costs (units):")
    print(f"    list playlists / items . {COST_PLAYLISTS_LIST} / {COST_ITEMS_LIST}")
    print(f"    create / delete playlist {COST_PLAYLISTS_INSERT} / {COST_PLAYLISTS_DELETE}")
    print(f"    add / delete video ..... {COST_ITEMS_INSERT} / {COST_ITEMS_DELETE}")
    print("\n  The default daily quota is 10,000 units (resets midnight PT).")
    print("  Tip: cloning a 3,000-video playlist costs ~150,000 units, so it is")
    print("  copied incrementally across days via a checkpoint.\n")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytmgr",
        description="Manage YouTube playlists from the command line.",
        epilog=(
            "Before using this tool you must:\n"
            "  1. Create a Google Cloud project and enable the YouTube Data API v3\n"
            "  2. Create OAuth 2.0 credentials (Desktop application type)\n"
            "  3. Set CLIENT_ID and CLIENT_SECRET in a .env file\n"
            "  4. Run 'ytmgr auth' to authenticate once"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- auth ---
    p_auth = sub.add_parser(
        "auth", help="Authenticate with Google / revoke stored token"
    )
    p_auth.add_argument(
        "--revoke",
        action="store_true",
        help="Remove stored token instead of authenticating",
    )

    # --- create-playlist / cp ---
    p_create = sub.add_parser(
        "create-playlist",
        aliases=["cp"],
        help="Create a new playlist",
    )
    p_create.add_argument("title", type=str, help="Playlist title")
    p_create.add_argument(
        "--description",
        "-d",
        type=str,
        default="",
        help="Playlist description",
    )
    p_create.add_argument(
        "--privacy",
        "-p",
        type=str,
        default="private",
        choices=("private", "unlisted", "public"),
        help="Privacy status (default: private)",
    )

    # --- delete-playlist / dp ---
    p_del_pl = sub.add_parser(
        "delete-playlist",
        aliases=["dp"],
        help="Delete a playlist by ID",
    )
    p_del_pl.add_argument("playlist_id", type=str, help="Playlist ID to delete")

    # --- list-playlists / lp ---
    p_lp = sub.add_parser(
        "list-playlists",
        aliases=["lp"],
        help="List all your playlists",
    )

    # --- list-items / li ---
    p_li = sub.add_parser(
        "list-items",
        aliases=["li"],
        help="List all videos in a playlist",
    )
    p_li.add_argument("playlist_id", type=str, help="Playlist ID")

    # --- add ---
    p_add = sub.add_parser(
        "add", help="Add video(s) to a playlist (by URL or video ID)"
    )
    p_add.add_argument("playlist_id", type=str, help="Playlist ID")
    p_add.add_argument(
        "video_ids",
        type=str,
        nargs="+",
        help="YouTube video URL(s) or ID(s)",
    )
    p_add.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Proceed even if the estimated cost exceeds today's remaining quota",
    )

    # --- delete-items / di ---
    p_di = sub.add_parser(
        "delete-items",
        aliases=["di"],
        help="Delete specific items (video entries) from a playlist",
    )
    p_di.add_argument("playlist_id", type=str, help="Playlist ID")
    p_di.add_argument(
        "item_ids",
        type=str,
        nargs="+",
        help="Playlist item ID(s) to delete (use `list-items` to find them)",
    )
    p_di.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Proceed even if the estimated cost exceeds today's remaining quota",
    )

    # --- tui ---
    p_tui = sub.add_parser(
        "tui",
        help="Launch the interactive terminal UI",
    )

    # --- clone-playlist / cl ---
    p_clone = sub.add_parser(
        "clone-playlist",
        aliases=["cl"],
        help="Clone an existing playlist (copy all videos into a new playlist)",
    )
    p_clone.add_argument("source_id", type=str, help="Source playlist ID to clone")
    p_clone.add_argument("title", type=str, help="Title for the new playlist")
    p_clone.add_argument(
        "--description",
        "-d",
        type=str,
        default="",
        help="Description for the new playlist",
    )
    p_clone.add_argument(
        "--privacy",
        "-p",
        type=str,
        default="private",
        choices=("private", "unlisted", "public"),
        help="Privacy status for the new playlist (default: private)",
    )
    p_clone.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore any saved checkpoint and start a fresh clone",
    )
    p_clone.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="Copy duplicate source videos instead of skipping them (costs more quota)",
    )
    p_clone.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Add at most N videos in this run (useful for large/quota-bound clones)",
    )
    p_clone.add_argument(
        "--batch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force batched inserts on/off (default: use YTMGR_USE_BATCH)",
    )

    # --- quota ---
    sub.add_parser(
        "quota",
        help="Show today's API quota usage and operation costs",
    )

    # --- dedup ---
    p_dedup = sub.add_parser(
        "dedup",
        help="Remove duplicate videos from a playlist (keeps oldest occurrence)",
    )
    p_dedup.add_argument("playlist_id", type=str, help="Playlist ID")
    p_dedup.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _configure_output() -> None:
    """Best-effort UTF-8 output so Unicode messages never crash on Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    # Map command (including aliases) to handler
    cmd_map = {
        "auth": cmd_auth,
        "create-playlist": cmd_create_playlist,
        "cp": cmd_create_playlist,
        "delete-playlist": cmd_delete_playlist,
        "dp": cmd_delete_playlist,
        "list-playlists": cmd_list_playlists,
        "lp": cmd_list_playlists,
        "list-items": cmd_list_items,
        "li": cmd_list_items,
        "tui": cmd_tui,
        "clone-playlist": cmd_clone_playlist,
        "cl": cmd_clone_playlist,
        "add": cmd_add,
        "delete-items": cmd_delete_items,
        "di": cmd_delete_items,
        "dedup": cmd_dedup,
        "quota": cmd_quota,
    }

    handler = cmd_map.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    # Commands that work without a YouTube service (the TUI manages its own auth)
    if args.command in ("auth", "tui", "quota"):
        return handler(None, args)

    # All other commands need an authenticated service
    try:
        service = get_authenticated_service()
    except Exception as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        return 1

    # Surface throttle/retry/quota activity on stderr so it never corrupts stdout.
    def _on_retry(message: str) -> None:
        print(f"  [retry] {message}", file=sys.stderr)

    def _on_quota_warning(message: str) -> None:
        print(f"  {message}", file=sys.stderr)

    client = YouTubeClient(
        service,
        on_retry=_on_retry,
        on_quota_warning=_on_quota_warning,
        quota=QuotaTracker(),
    )

    try:
        return handler(client, args)
    except YouTubeAPIError as e:
        print(f"Error: {e.friendly()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())