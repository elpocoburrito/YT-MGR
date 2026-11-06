"""Textual TUI application for ytmgr."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Log,
    Static,
)

from ytmgr.auth import authenticate, revoke_token
from ytmgr.client import YouTubeClient
from ytmgr.errors import YouTubeAPIError
from ytmgr.quota import QuotaTracker

# Shared across screens so quota usage is consistent within a session.
_QUOTA = QuotaTracker()

# ---------------------------------------------------------------------------
# Thread-pool for running blocking API calls
# ---------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=1)


async def _run_blocking(func, *args, **kwargs):
    """Run a synchronous function in a thread and await the result."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# Menu definition
# ---------------------------------------------------------------------------

# (item id, label, target screen name)
MENU_ITEMS: list[tuple[str, str, str]] = [
    ("lp", "📋  List Playlists", "playlist_list"),
    ("li", "🔍  View Playlist Items", "playlist_items_prompt"),
    ("cp", "✨  Create Playlist", "create_playlist"),
    ("cl", "📦  Clone Playlist", "clone_playlist"),
    ("add", "➕  Add Videos to Playlist", "add_videos"),
    ("dedup", "🧹  Remove Duplicates", "dedup"),
    ("auth", "🔑  Authentication", "auth"),
]

_MENU_SCREEN_MAP = {key: screen for key, _, screen in MENU_ITEMS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _back_bar(label: str = "⬅  Back") -> ComposeResult:
    """Yield a right/bottom aligned back button for mouse users."""
    with Static(classes="actions"):
        yield Button(label, id="back-btn")


def _fmt_error(exc: Exception) -> str:
    """Return a friendly, user-facing message for an exception."""
    if isinstance(exc, YouTubeAPIError):
        return exc.friendly()
    return str(exc)


def _make_progress_logger(screen: Screen):
    """Build a progress callback that safely writes to the screen's log."""
    log = screen.query_one("#log", Log)

    def _progress(message: str, current: int = 0, total=None) -> None:
        try:
            screen.app.call_from_thread(log.write, f"  {message}")
        except Exception:
            pass

    return _progress


def _ensure_client(screen: Screen) -> YouTubeClient | None:
    """Try to build a YouTubeClient. Return None if auth fails."""
    from ytmgr.auth import get_authenticated_service

    try:
        service = get_authenticated_service()
    except Exception as exc:
        log = screen.query_one("#log", Log)
        log.write(f"[red]Authentication failed:[/red] {exc}")
        return None

    log = screen.query_one("#log", Log)

    def _on_retry(message: str) -> None:
        # Called from the worker thread, so marshal onto the app thread.
        try:
            screen.app.call_from_thread(log.write, f"[yellow]⏳ {message}[/yellow]")
        except Exception:
            pass

    def _on_quota_warning(message: str) -> None:
        try:
            screen.app.call_from_thread(log.write, f"[yellow]⚠ {message}[/yellow]")
        except Exception:
            pass

    return YouTubeClient(
        service,
        on_retry=_on_retry,
        on_quota_warning=_on_quota_warning,
        quota=_QUOTA,
    )


# ---------------------------------------------------------------------------
# Main menu screen
# ---------------------------------------------------------------------------


class MenuScreen(Screen):
    """Main menu with keyboard-navigable list."""

    CSS = """
    #menu-title {
        text-style: bold;
        color: $accent;
        content-align: center top;
        padding: 1;
        height: 3;
    }
    #menu-list {
        height: auto;
        max-height: 1fr;
        margin: 1 4;
        border: solid $border;
    }
    #menu-list ListItem {
        padding: 0 1;
    }
    #menu-list ListItem Label {
        margin: 0;
    }
    #auth-status {
        color: $text-muted;
        content-align: center middle;
        height: 1;
        margin: 1 4;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("🎬 YouTube Playlist Manager", id="menu-title")
        yield ListView(
            *(ListItem(Label(label), id=key) for key, label, _ in MENU_ITEMS),
            id="menu-list",
        )
        with Static(classes="actions"):
            yield Button("🚪  Quit", id="quit-btn", variant="error")
        yield Static(id="auth-status")

    def on_screen_mount(self) -> None:
        self.query_one("#menu-list", ListView).focus()
        self._update_auth_status()
        self._maybe_verify_quota()

    def on_screen_resume(self) -> None:
        # Re-focus the list whenever we come back to the menu
        self.query_one("#menu-list", ListView).focus()
        self._update_auth_status()
        self._maybe_verify_quota()

    def _maybe_verify_quota(self) -> None:
        """Verify quota against the live API once per TUI session."""
        from ytmgr.config import TOKEN_FILE

        if getattr(self.app, "_quota_verified", False):
            return
        if not TOKEN_FILE.exists():
            return
        self.app._quota_verified = True
        self.run_worker(self._verify_quota(), description="Verifying quota…")

    async def _verify_quota(self) -> None:
        """Probe the real API to confirm reachability / quota exhaustion."""
        from ytmgr.auth import get_authenticated_service

        status_w = self.query_one("#auth-status", Static)
        status_w.update("⏳ Verifying quota with the YouTube API…")

        try:
            service = await _run_blocking(get_authenticated_service)
            client = YouTubeClient(service, min_interval=0, quota=_QUOTA)
            result = await _run_blocking(client.probe_quota)
        except Exception as exc:  # noqa: BLE001
            self.app._quota_verify_state = "error"
            self.app._quota_verify_error = str(exc)
            self._update_auth_status()
            return

        if result.get("exhausted"):
            self.app._quota_verify_state = "exhausted"
        elif result.get("ok"):
            self.app._quota_verify_state = "ok"
        else:
            self.app._quota_verify_state = "error"
            self.app._quota_verify_error = result.get("error", "unknown error")
        self._update_auth_status()

    def _update_auth_status(self) -> None:
        """Render the footer from the current quota snapshot + verified status."""
        from ytmgr.config import TOKEN_FILE

        status_w = self.query_one("#auth-status", Static)
        if not TOKEN_FILE.exists():
            status_w.update("❌ Not authenticated — use 'Authentication' first")
            return

        snap = _QUOTA.snapshot()
        estimate = (
            f"local estimate {snap['used']}/{snap['limit']} "
            f"({snap['remaining']} left)"
        )
        state = getattr(self.app, "_quota_verify_state", "")

        if state == "exhausted":
            status_w.update(
                "❌ Google API reports the daily quota is EXHAUSTED "
                "(resets at midnight Pacific Time)"
            )
        elif state == "ok":
            status_w.update(
                f"✅ API reachable — quota available    ·    {estimate}"
            )
        elif state == "error":
            err = getattr(self.app, "_quota_verify_error", "unknown error")
            status_w.update(f"⚠ Could not verify quota: {err}    ·    {estimate}")
        else:
            status_w.update(f"✅ Authenticated — token cached    ·    {estimate}")

    @on(ListView.Selected, "#menu-list")
    def _menu_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        screen_name = _MENU_SCREEN_MAP.get(item_id)
        if screen_name:
            self.app.push_screen(screen_name)


# ---------------------------------------------------------------------------
# Playlist list screen
# ---------------------------------------------------------------------------


class PlaylistListScreen(Screen):
    """Fetch and display all playlists."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]📋 Your Playlists[/bold]", id="screen-title")
        yield Log(id="log")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self.run_worker(self._load, description="Loading playlists…")

    async def _load(self) -> None:
        log = self.query_one("#log", Log)
        log.write("Fetching playlists…")

        client = _ensure_client(self)
        if client is None:
            return

        try:
            resp = await _run_blocking(client.list_playlists)
            items = resp.get("items", [])
            if not items:
                log.write("\n[yellow]No playlists found.[/yellow]")
            else:
                log.write(f"\n[bold]{len(items)} playlist(s):[/bold]\n")
                for p in items:
                    s = p.get("snippet", {})
                    title = s.get("title", "(untitled)")
                    pid = p.get("id", "?")
                    count = p.get("contentDetails", {}).get("itemCount", "?")
                    log.write(f"  {title}  [[dim]{pid}[/dim]]  ({count} videos)")
        except Exception as exc:
            log.write(f"\n[red]Error:[/red] {_fmt_error(exc)}")

        log.write("\n────────────────────────")
        log.write("Press [bold]Esc[/bold] / [bold]Backspace[/bold] or the Back button to return.")


# ---------------------------------------------------------------------------
# Playlist items screen
# ---------------------------------------------------------------------------


class PlaylistItemsPromptScreen(Screen):
    """Ask for a playlist ID, then show its items."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]🔍 Playlist Items[/bold]", id="screen-title")
        yield Label("Enter Playlist ID:")
        yield Input(placeholder="e.g. PL1234567890abcdef", id="pid-input")
        yield Button("▶ View Items", id="go", variant="primary")
        yield Log(id="log")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self.query_one("#pid-input", Input).focus()

    @on(Button.Pressed, "#go")
    @on(Input.Submitted, "#pid-input")
    async def fetch(self) -> None:
        inp = self.query_one("#pid-input", Input)
        pid = inp.value.strip()
        if not pid:
            return
        log = self.query_one("#log", Log)
        log.write(f"Fetching items for [bold]{pid}[/bold]…")

        client = _ensure_client(self)
        if client is None:
            return

        try:
            items = await _run_blocking(
                client.list_all_items, pid, progress_cb=_make_progress_logger(self)
            )
            if not items:
                log.write("\n[yellow]No videos in this playlist.[/yellow]")
            else:
                log.write(f"\n[bold]{len(items)} video(s):[/bold]\n")
                for idx, item in enumerate(items):
                    s = item.get("snippet", {})
                    title = s.get("title", "(unknown)")
                    vid = s.get("resourceId", {}).get("videoId", "?")
                    iid = item.get("id", "?")
                    log.write(
                        f"  #{idx+1:>3d}  {title[:60]:60s}  "
                        f"[[dim]{vid}[/dim]]  [[dim]{iid}[/dim]]"
                    )
        except Exception as exc:
            log.write(f"\n[red]Error:[/red] {_fmt_error(exc)}")

        log.write("\n────────────────────────")
        log.write("Press [bold]Esc[/bold] / [bold]Backspace[/bold] or the Back button to return.")


# ---------------------------------------------------------------------------
# Create playlist screen
# ---------------------------------------------------------------------------


class CreatePlaylistScreen(Screen):
    """Form to create a new playlist."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]✨ Create Playlist[/bold]", id="screen-title")
        yield Label("Title:")
        yield Input(placeholder="My Awesome Mix", id="title-input")
        yield Label("Description (optional):")
        yield Input(placeholder="Description…", id="desc-input")
        yield Label("Privacy:")
        yield Input(value="private", placeholder="private / unlisted / public", id="privacy-input")
        yield Button("✅ Create", id="create", variant="primary")
        yield Log(id="log")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self.query_one("#title-input", Input).focus()

    @on(Button.Pressed, "#create")
    async def do_create(self) -> None:
        title = self.query_one("#title-input", Input).value.strip()
        if not title:
            self.query_one("#log", Log).write("[red]Title is required.[/red]")
            return
        desc = self.query_one("#desc-input", Input).value.strip()
        privacy = self.query_one("#privacy-input", Input).value.strip().lower()
        if privacy not in ("private", "unlisted", "public"):
            privacy = "private"

        client = _ensure_client(self)
        if client is None:
            return

        log = self.query_one("#log", Log)
        log.write(f"Creating playlist '{title}'…")
        try:
            result = await _run_blocking(client.create_playlist, title, desc, privacy)
            pid = result.get("id", "?")
            log.write(f"\n[green]✅ Created:[/green] {title}  [[dim]{pid}[/dim]]")
        except Exception as exc:
            log.write(f"\n[red]Error:[/red] {_fmt_error(exc)}")


# ---------------------------------------------------------------------------
# Clone playlist screen
# ---------------------------------------------------------------------------


class ClonePlaylistScreen(Screen):
    """Form to clone a playlist."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]📦 Clone Playlist[/bold]", id="screen-title")
        yield Label("Source Playlist ID:")
        yield Input(placeholder="PL…", id="source-input")
        yield Label("New Playlist Title:")
        yield Input(placeholder="My Cloned Mix", id="title-input")
        yield Label("Description (optional):")
        yield Input(placeholder="Description…", id="desc-input")
        yield Label("Privacy:")
        yield Input(value="private", placeholder="private / unlisted / public", id="privacy-input")
        yield Button("✅ Clone", id="clone", variant="primary")
        yield Log(id="log")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self.query_one("#source-input", Input).focus()

    @on(Button.Pressed, "#clone")
    async def do_clone(self) -> None:
        src = self.query_one("#source-input", Input).value.strip()
        title = self.query_one("#title-input", Input).value.strip()
        if not src or not title:
            self.query_one("#log", Log).write("[red]Source ID and Title are required.[/red]")
            return
        desc = self.query_one("#desc-input", Input).value.strip()
        privacy = self.query_one("#privacy-input", Input).value.strip().lower()
        if privacy not in ("private", "unlisted", "public"):
            privacy = "private"

        client = _ensure_client(self)
        if client is None:
            return

        log = self.query_one("#log", Log)
        log.write(f"Cloning playlist [bold]{src}[/bold] → '{title}' …")
        try:
            result = await _run_blocking(
                client.clone_playlist,
                src,
                title,
                desc,
                privacy,
                progress_cb=_make_progress_logger(self),
            )
            new_id = result.get("id", "?")
            stats = result.get("_clone_stats", {})
            log.write(
                f"\n[green]✅ Clone target:[/green] {title}  [[dim]{new_id}[/dim]]\n"
                f"   Source: {stats.get('total_source', 0)}   "
                f"Added: {stats.get('added', 0)}   "
                f"Already: {stats.get('already_added', 0)}   "
                f"Errors: {stats.get('errors', 0)}"
            )
            if stats.get("complete"):
                log.write("   [green]Clone complete.[/green]")
            else:
                if stats.get("quota_exhausted"):
                    log.write("   [yellow]Paused: daily quota reached.[/yellow]")
                log.write(
                    "   [yellow]Incomplete — re-run the same clone to resume.[/yellow]"
                )
        except Exception as exc:
            log.write(f"\n[red]Error:[/red] {_fmt_error(exc)}")


# ---------------------------------------------------------------------------
# Add videos screen
# ---------------------------------------------------------------------------


class AddVideosScreen(Screen):
    """Form to add videos to a playlist."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]➕ Add Videos to Playlist[/bold]", id="screen-title")
        yield Label("Playlist ID:")
        yield Input(placeholder="PL…", id="pid-input")
        yield Label("Video URL(s) or ID(s) — space or comma separated:")
        yield Input(placeholder="https://youtube.com/watch?v=xxxx", id="vids-input")
        yield Button("✅ Add", id="add", variant="primary")
        yield Log(id="log")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self.query_one("#pid-input", Input).focus()

    @on(Button.Pressed, "#add")
    async def do_add(self) -> None:
        pid = self.query_one("#pid-input", Input).value.strip()
        raw = self.query_one("#vids-input", Input).value.strip()
        if not pid or not raw:
            self.query_one("#log", Log).write(
                "[red]Playlist ID and at least one video URL are required.[/red]"
            )
            return

        from ytmgr.client import extract_video_id

        video_ids = []
        for token in raw.replace(",", " ").split():
            token = token.strip()
            if not token:
                continue
            try:
                video_ids.append(extract_video_id(token))
            except ValueError:
                pass

        if not video_ids:
            self.query_one("#log", Log).write(
                "[red]No valid video IDs could be extracted.[/red]"
            )
            return

        client = _ensure_client(self)
        if client is None:
            return

        log = self.query_one("#log", Log)
        log.write(f"Adding [bold]{len(video_ids)}[/bold] video(s)…")

        ok = 0
        fail = 0
        for vid in video_ids:
            try:
                await _run_blocking(client.add_item, pid, vid)
                log.write(f"  [green]✓[/green] {vid}")
                ok += 1
            except Exception as e:
                log.write(f"  [red]✗[/red] {vid} — {e}")
                fail += 1

        log.write(f"\n[bold]Done:[/bold] {ok} added, {fail} failed.")


# ---------------------------------------------------------------------------
# Dedup screen
# ---------------------------------------------------------------------------


class DedupScreen(Screen):
    """Analyse and remove duplicates in a playlist."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]🧹 Remove Duplicates[/bold]", id="screen-title")
        yield Label("Playlist ID:")
        yield Input(placeholder="PL…", id="pid-input")
        yield Button("🔍 Analyse", id="analyse", variant="primary")
        yield Log(id="log")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self.query_one("#pid-input", Input).focus()

    @on(Button.Pressed, "#analyse")
    async def do_analyse(self) -> None:
        pid = self.query_one("#pid-input", Input).value.strip()
        if not pid:
            return

        client = _ensure_client(self)
        if client is None:
            return

        log = self.query_one("#log", Log)
        log.write(f"Analysing playlist [bold]{pid}[/bold]…")

        try:
            groups = await _run_blocking(
                client.find_duplicate_items,
                pid,
                progress_cb=_make_progress_logger(self),
            )
        except Exception as exc:
            log.write(f"\n[red]Error:[/red] {_fmt_error(exc)}")
            return

        if not groups:
            log.write("\n[green]✅ No duplicates found![/green]")
            return

        total_dups = sum(len(g) - 1 for g in groups)
        log.write(
            f"\n[yellow]Found {len(groups)} video(s) with duplicates "
            f"({total_dups} to remove):[/yellow]\n"
        )
        for gi, group in enumerate(groups):
            vid = group[0].get("snippet", {}).get("resourceId", {}).get("videoId", "?")
            title = group[0].get("snippet", {}).get("title", "?")
            log.write(f"\n  [bold]#{gi+1}[/bold] {title}  ([[dim]{vid}[/dim]], {len(group)}×)")
            for i, item in enumerate(group):
                iid = item.get("id", "?")
                marker = "[green]KEEP[/green]" if i == 0 else "[red]DEL[/red]"
                log.write(f"    {marker}  [[dim]{iid}[/dim]]")

        log.write("\n────────────────────────")
        # Add the remove button once, if not already present
        if not self.query("#remove"):
            await self.mount(
                Button("🗑 Remove Duplicates Now", id="remove", variant="error"),
                after=self.query_one("#analyse"),
            )

    @on(Button.Pressed, "#remove")
    async def do_remove(self) -> None:
        pid = self.query_one("#pid-input", Input).value.strip()
        client = _ensure_client(self)
        if client is None:
            return

        log = self.query_one("#log", Log)
        log.write("Removing duplicates…")

        try:
            groups = await _run_blocking(
                client.find_duplicate_items, pid, progress_cb=_make_progress_logger(self)
            )
            deleted = await _run_blocking(
                client.remove_duplicates,
                pid,
                progress_cb=_make_progress_logger(self),
                groups=groups,
            )
            log.write(f"\n[green]✅ Removed {len(deleted)} duplicate(s).[/green]")
        except Exception as exc:
            log.write(f"\n[red]Error:[/red] {_fmt_error(exc)}")

        try:
            self.query_one("#remove", Button).remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Auth screen
# ---------------------------------------------------------------------------


class AuthScreen(Screen):
    """Authentication management screen."""

    def compose(self) -> ComposeResult:
        yield Static("[bold]🔑 Authentication[/bold]", id="screen-title")
        yield Static(id="auth-info")
        yield Log(id="log")
        with Static(classes="actions"):
            yield Button("🔐 Authenticate (OAuth)", id="auth-do", variant="primary")
            yield Button("🗑 Revoke Token", id="revoke", variant="error")
        yield from _back_bar()

    def on_screen_mount(self) -> None:
        self._update_status()

    def _update_status(self) -> None:
        from ytmgr.config import TOKEN_FILE

        info = self.query_one("#auth-info", Static)
        if TOKEN_FILE.exists():
            info.update("[green]✅ Authenticated[/green] — token cached on disk")
        else:
            info.update("[yellow]❌ No cached token[/yellow]")

    @on(Button.Pressed, "#auth-do")
    def do_auth(self) -> None:
        log = self.query_one("#log", Log)
        try:
            authenticate(force=True)
            log.write("[green]✅ Authentication successful![/green]")
            self._reset_quota_verification()
            self._update_status()
        except Exception as exc:
            log.write(f"[red]Error:[/red] {exc}")

    @on(Button.Pressed, "#revoke")
    def do_revoke(self) -> None:
        revoke_token()
        log = self.query_one("#log", Log)
        log.write("[yellow]Token removed (if it existed).[/yellow]")
        self._reset_quota_verification()
        self._update_status()

    def _reset_quota_verification(self) -> None:
        """Force a fresh API quota probe when returning to the menu."""
        self.app._quota_verified = False
        self.app._quota_verify_state = ""
        self.app._quota_verify_error = ""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

SCREENS = {
    "menu": MenuScreen,
    "playlist_list": PlaylistListScreen,
    "playlist_items_prompt": PlaylistItemsPromptScreen,
    "create_playlist": CreatePlaylistScreen,
    "clone_playlist": ClonePlaylistScreen,
    "add_videos": AddVideosScreen,
    "dedup": DedupScreen,
    "auth": AuthScreen,
}


class YtMgrTui(App):
    """Textual TUI for ytmgr."""

    CSS = """
    Screen {
        background: $surface;
    }
    #screen-title {
        text-style: bold;
        color: $accent;
        padding: 1 2;
    }
    #log {
        border: solid $border;
        height: 1fr;
        margin: 0 2;
    }
    Label {
        margin: 0 2;
        color: $text;
    }
    Input {
        margin: 0 2 1 2;
    }
    Button {
        margin: 1 2;
    }
    .actions {
        height: auto;
        align: center middle;
    }
    #auth-status {
        color: $text-muted;
    }
    #auth-info {
        padding: 0 2;
    }
    """

    BINDINGS = [
        # Escape / Backspace go back one menu level; on the main menu Escape quits.
        Binding("escape", "go_back", "Back / Quit"),
        Binding("backspace", "go_back", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Quota verification runs once per session (on first menu mount).
        self._quota_verified = False
        self._quota_verify_state = ""  # "ok" | "exhausted" | "error"
        self._quota_verify_error = ""
        for name, screen_cls in SCREENS.items():
            self.install_screen(screen_cls(), name)

    def on_mount(self) -> None:
        self.push_screen("menu")

    # -- Global navigation ---------------------------------------------------

    def action_go_back(self) -> None:
        """Go back to the previous screen, or quit when on the main menu."""
        if isinstance(self.screen, MenuScreen):
            self.exit()
        elif len(self.screen_stack) > 1:
            self.pop_screen()

    @on(Button.Pressed, "#back-btn")
    def _back_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_go_back()

    @on(Button.Pressed, "#quit-btn")
    def _quit_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.exit()