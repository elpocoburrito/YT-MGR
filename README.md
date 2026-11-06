# YouTube Playlist Manager (`ytmgr`)

A command-line tool (with an interactive TUI) for managing YouTube playlists
using the **YouTube Data API v3**. Create, clone, deduplicate, and manage
playlists — including very large ones (3000+ videos) — while staying friendly to
your API quota.

---

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Google API setup (OAuth)](#google-api-setup-oauth)
  - [1. Create a Google Cloud project](#1-create-a-google-cloud-project)
  - [2. Enable the YouTube Data API v3](#2-enable-the-youtube-data-api-v3)
  - [3. Configure the OAuth consent screen](#3-configure-the-oauth-consent-screen)
  - [4. Create OAuth 2.0 credentials](#4-create-oauth-20-credentials)
  - [5. Configure the app](#5-configure-the-app)
  - [6. Authenticate](#6-authenticate)
- [Quick start](#quick-start)
- [Commands](#commands)
  - [Playlists](#playlists)
  - [Videos in a playlist](#videos-in-a-playlist)
  - [Cloning](#cloning)
  - [Duplicates](#duplicates)
  - [Interactive TUI](#interactive-tui)
  - [Quota usage](#quota-usage)
- [Quota, throttling & large playlists](#quota-throttling--large-playlists)
- [Configuration reference](#configuration-reference)
- [Error handling](#error-handling)
- [Where data is stored](#where-data-is-stored)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Project structure](#project-structure)
- [License](#license)

---

## Features

- **Create** playlists (public, unlisted, or private)
- **List** all playlists (fully paginated)
- **List** all videos in a playlist (paginated, 50 per request)
- **Add** videos to a playlist by URL or video ID
- **Delete** individual entries or an entire playlist
- **Clone** a playlist into a new one with a different name
- **Deduplicate** a playlist (keeps the oldest occurrence)
- **Interactive TUI** powered by [Textual](https://textual.textualize.io/)
- **Quota-aware**: tracks daily usage, estimates costs, and refuses requests
  that would exceed the budget
- **Throttled & retried**: rate limiting plus exponential backoff with jitter
- **Batched & cached**: bulk inserts use batched HTTP requests; repeated reads
  are cached per session
- **Large playlists**: incremental, resumable clones that can span multiple days

## Requirements

- **Python 3.10+**
- A **Google Cloud project** with the **YouTube Data API v3** enabled
- **OAuth 2.0 credentials** of type **Desktop app**

## Installation

```bash
git clone <your-repo-url> yt-mgr
cd yt-mgr

python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt
```

Alternatively, install it as a package (provides the `ytmgr` command):

```bash
pip install -e .
```

---

## Google API setup (OAuth)

`ytmgr` needs its own OAuth client so it can act on **your** YouTube account.
This is a one-time setup. The steps below use the current Google Cloud Console.

### 1. Create a Google Cloud project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project selector at the top, then **New Project**.
3. Give it a name (e.g. `ytmgr`) and click **Create**.
4. Make sure the new project is selected.

### 2. Enable the YouTube Data API v3

1. Go to **APIs & Services → Library**.
2. Search for **YouTube Data API v3**.
3. Open it and click **Enable**.

> The API has a default **daily quota of 10,000 units** per project. See
> [Quota, throttling & large playlists](#quota-throttling--large-playlists).

### 3. Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
   (in newer consoles this may appear under **Google Auth Platform**).
2. Choose the **User type**:
   - **External** — for a normal personal Google account (required unless you
     use Google Workspace).
   - **Internal** — only if your Google Workspace organization owns the project.
3. Fill in the required **App information**:
   - **App name**: e.g. `ytmgr`
   - **User support email**: your email
   - **Developer contact email**: your email
4. On the **Scopes** step, add:
   ```
   https://www.googleapis.com/auth/youtube
   ```
   This scope allows the tool to manage your YouTube account (playlists).
   For a YouTube-only tool this is the single scope needed.
5. On the **Test users** step (for an *External* app in **Testing** status),
   click **Add users** and add the Google account that owns the playlists.
   Google allows up to 100 test users in testing mode.
6. Save. Leaving the app in **Testing** status is fine for personal use — you
   do **not** need to publish or get verified.

> If you skip adding yourself as a **test user**, authentication will fail with
> “Access blocked” / “This app has not completed verification”.

### 4. Create OAuth 2.0 credentials

1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → OAuth client ID**.
3. **Application type**: choose **Desktop app**.
4. Name it (e.g. `ytmgr-desktop`) and click **Create**.
5. Copy the **Client ID** and **Client secret**.

> A **Desktop app** client uses a loopback redirect
> (`http://localhost:<random port>`) handled automatically by the library, so you
> do **not** need to register a redirect URI. Do not use the “Web application”
> type unless you add `http://localhost` as an authorized redirect URI.

### 5. Configure the app

Copy the example env file and add your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
CLIENT_ID=your_client_id_here.apps.googleusercontent.com
CLIENT_SECRET=your_client_secret_here
```

> **Do not commit `.env`.** It is already listed in `.gitignore`.

### 6. Authenticate

```bash
python -m ytmgr auth
```

This opens your browser for Google's OAuth consent. Approve the request; the
resulting token is saved to `~/.ytmgr/token.json` (outside the repository).

To sign out or switch accounts:

```bash
python -m ytmgr auth --revoke   # delete the cached token
python -m ytmgr auth            # sign in again
```

---

## Quick start

```bash
# 1. list your playlists (get their IDs)
python -m ytmgr lp

# 2. create a playlist
python -m ytmgr cp "Road Trip" --privacy unlisted

# 3. add videos
python -m ytmgr add PLAYLIST_ID "https://youtu.be/dQw4w9WgXcQ"

# 4. list its videos
python -m ytmgr li PLAYLIST_ID

# 5. clone / deduplicate
python -m ytmgr cl PLAYLIST_ID "Road Trip (copy)"
python -m ytmgr dedup PLAYLIST_ID

# 6. or use the interactive TUI
python -m ytmgr tui
```

---

## Commands

Run `python -m ytmgr --help` (or `python -m ytmgr <command> --help`) for details.

### Playlists

| Command | Alias | Description |
|---|---|---|
| `list-playlists` | `lp` | List all your playlists (paginated) |
| `create-playlist TITLE` | `cp` | Create a playlist (`--description`, `--privacy`) |
| `delete-playlist PLAYLIST_ID` | `dp` | Delete a playlist (asks for confirmation) |

```bash
python -m ytmgr lp
python -m ytmgr cp "My Mix" -d "Great tracks" -p public
python -m ytmgr dp PLAYLIST_ID
```

### Videos in a playlist

| Command | Alias | Description |
|---|---|---|
| `list-items PLAYLIST_ID` | `li` | List all videos (shows item IDs) |
| `add PLAYLIST_ID URL_OR_ID...` | — | Add one or more videos |
| `delete-items PLAYLIST_ID ITEM_ID...` | `di` | Delete specific entries |

```bash
python -m ytmgr li PLAYLIST_ID
python -m ytmgr add PLAYLIST_ID "https://youtu.be/dQw4w9WgXcQ" "ANOTHER_ID"
python -m ytmgr di PLAYLIST_ID ITEM_ID1 ITEM_ID2
```

`list-items` prints each entry's **playlist item ID** in `[...]` at the end of the
row — that is what `delete-items` expects (not the video ID).

### Cloning

```bash
python -m ytmgr clone-playlist SOURCE_ID "New Playlist Name"
python -m ytmgr cl SOURCE_ID "New Playlist Name"
```

| Flag | Description |
|---|---|
| `--description` / `-d` | Description for the new playlist |
| `--privacy` / `-p` | `private` / `unlisted` / `public` |
| `--no-resume` | Ignore a saved checkpoint and start fresh |
| `--keep-duplicates` | Copy duplicate source videos (costs more quota) |
| `--limit N` | Add at most `N` videos in this run |
| `--batch` / `--no-batch` | Force batched inserts on/off |

Clones are **incremental and resumable**: if the daily quota runs out, a
checkpoint is saved and re-running the same command continues where it stopped.
See [Large playlists](#quota-throttling--large-playlists).

### Duplicates

```bash
python -m ytmgr dedup PLAYLIST_ID        # shows duplicates, asks to confirm
python -m ytmgr dedup PLAYLIST_ID -y     # skip confirmation
```

Duplicate videos are removed **from oldest to newest**: the oldest occurrence is
kept and later occurrences are deleted.

### Interactive TUI

```bash
python -m ytmgr tui
```

Keyboard navigation: **↑/↓** move, **Enter** select, **Esc/Backspace** go back
(**Esc** on the main menu quits). A **Back** button is available for mouse users.

On launch, the TUI performs a live quota check against the YouTube API using the
cheapest request (one `playlists.list`, costing **1 unit**):

- `✅ API reachable — quota available` plus the local usage estimate.
- `❌ Google API reports the daily quota is EXHAUSTED` if the API returns
  `403 quotaExceeded` / `dailyLimitExceeded`.

> The YouTube Data API has **no endpoint that returns your absolute remaining
> quota**. The probe verifies reachability and exhaustion; the numeric value is a
> locally tracked estimate.

### Quota usage

```bash
python -m ytmgr quota
```

Shows today's tracked usage and the unit cost of each operation.

---

## Quota, throttling & large playlists

The YouTube Data API enforces a **daily quota** (default **10,000 units** per
Cloud project). Reads are cheap; **writes cost 50 units each**.

| Operation | Units |
|---|---|
| `playlists.list` / `playlistItems.list` (per page of 50) | 1 |
| `playlists.insert` / `playlists.delete` | 50 |
| `playlistItems.insert` / `playlistItems.delete` | 50 |

At 50 units per write, one day's quota allows roughly **200 inserts/deletes**.

### How `ytmgr` conserves quota

- **Caching** — repeated reads of the same playlist in a session are served from
  memory; caches are invalidated on mutation.
- **No double listing** — `dedup` reuses the listing it already fetched.
- **Minimal fields** — item listings request only `snippet`.
- **Max page size** — lists 50 items per request, the API maximum.
- **Batched inserts** — up to 50 inserts per HTTP request
  (`YTMGR_BATCH_SIZE`, `YTMGR_USE_BATCH`), with automatic fallback to single
  requests.
- **Skip duplicates on clone** — avoids paying 50 units per duplicate.
- **Local quota guard** — refuses a request that would exceed the daily budget
  (instead of triggering a server-side `403`), and warns at 80%.

### Worked example: 3,000-video playlist

Listing it costs ~60 units. Cloning it costs **~150,000 units** (3,000 × 50),
far beyond a single day's budget. `ytmgr` clones incrementally:

```bash
# Day 1 — copies until the quota runs out, then pauses and saves a checkpoint
python -m ytmgr cl SOURCE_ID "My Big Clone"

# Day 2, Day 3, ... — run the exact same command to continue
python -m ytmgr cl SOURCE_ID "My Big Clone"
```

Each successful insert is recorded in the checkpoint, so already-copied videos
are never added again. To cap a run manually:

```bash
python -m ytmgr cl SOURCE_ID "My Big Clone" --limit 150
```

### Batch requests and quota

Batching reduces the number of HTTP round trips (up to 50×) but **does not
reduce quota units** — each sub-request still costs its normal units. Throttling
and backoff protect against per-second rate limits; the quota guard protects the
daily budget.

---

## Configuration reference

All settings are optional and read from `.env` (or the environment).

| Variable | Default | Description |
|---|---|---|
| `CLIENT_ID` | — | OAuth 2.0 client ID (**required**) |
| `CLIENT_SECRET` | — | OAuth 2.0 client secret (**required**) |
| `YTMGR_MIN_INTERVAL` | `0.25` | Minimum seconds between requests (~4/sec) |
| `YTMGR_MAX_RETRIES` | `5` | Max retries for transient errors |
| `YTMGR_BACKOFF_BASE` | `1.0` | Exponential backoff base (seconds) |
| `YTMGR_BACKOFF_MAX` | `60.0` | Backoff cap (seconds) |
| `YTMGR_DAILY_QUOTA` | `10000` | Daily quota for your project |
| `YTMGR_QUOTA_WARN_RATIO` | `0.8` | Warn at this fraction of usage |
| `YTMGR_QUOTA_ENFORCE` | `1` | Block requests that would exceed the budget |
| `YTMGR_BATCH_SIZE` | `50` | Sub-requests per batch (1–50) |
| `YTMGR_USE_BATCH` | `1` | Use batched inserts for bulk operations |

See `.env.example` for a commented template.

---

## Error handling

Every API call goes through a throttled executor that:

- Enforces `YTMGR_MIN_INTERVAL` between requests.
- Retries **transient** failures with **exponential backoff + jitter**, honouring
  the server's `Retry-After` header:
  - `429 Too Many Requests`
  - `5xx` server errors
  - `403` with `rateLimitExceeded` / `userRateLimitExceeded` / `backendError`
  - network timeouts / connection errors
- Fails fast with an actionable message for **non-retryable** errors:
  - `403 quotaExceeded` / `dailyLimitExceeded`
  - `401` (authorization expired — re-run `ytmgr auth`)
  - `404` (not found)

Retry messages look like:
`[retry] add video …: HTTP 403 rateLimitExceeded — retry 1/5 in 2.3s`.

---

## Where data is stored

Everything user-specific lives **outside** the repository, under `~/.ytmgr/`:

| Path | Contents |
|---|---|
| `~/.ytmgr/token.json` | OAuth access/refresh token (permissions `0600` on Unix) |
| `~/.ytmgr/quota.json` | Local daily quota usage estimate |
| `~/.ytmgr/clones/` | Resumable clone checkpoints |

Delete `~/.ytmgr/` to fully reset the tool, or use `python -m ytmgr auth --revoke`
to remove only the token.

---

## Security notes

- `.env` contains your **client secret** and is listed in `.gitignore`. Never
  commit it, paste it in issues, or share it.
- `.env.example` contains only placeholders and is safe to commit.
- The OAuth token in `~/.ytmgr/token.json` grants access to your YouTube account.
  Treat it like a password and keep it outside version control.
- For a shared/public deployment, create a dedicated Google Cloud project and
  complete Google's OAuth verification for the `youtube` scope.
- The requested scope (`https://www.googleapis.com/auth/youtube`) allows managing
  your YouTube account; only grant it to code you trust.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing CLIENT_ID or CLIENT_SECRET` | Create `.env` from `.env.example` and fill in both values. |
| Browser shows **“Access blocked: This app has not completed verification”** | Add your Google account as a **test user** on the OAuth consent screen. |
| **`redirect_uri_mismatch`** | Use an OAuth client of type **Desktop app** (not “Web application”). |
| **`invalid_client`** | The client ID/secret in `.env` don't match the credentials. |
| **`403 quotaExceeded` / `dailyLimitExceeded`** | Daily quota used up. Wait for midnight **Pacific Time**, raise `YTMGR_DAILY_QUOTA` if your project has more quota, or request a quota increase in Cloud Console → IAM & Admin → Quotas. Clone progress is saved and resumable. |
| **`403 rateLimitExceeded`** | Increase `YTMGR_MIN_INTERVAL` (e.g. `1.0`) and retry. |
| **`401` / token errors** | `python -m ytmgr auth --revoke` then `python -m ytmgr auth`. |
| **No playlists listed** | Ensure you signed in with the account that owns them and that the YouTube Data API is enabled. |
| **`batch` errors** | Try `--no-batch` (or `YTMGR_USE_BATCH=0`). |

For quota increases: Google Cloud Console → **IAM & Admin → Quotas**, filter for
**YouTube Data API v3**, and request an increase.

---

## Development

Run the offline test suite (no network or credentials required):

```bash
python tests/test_optimisation.py
```

It exercises pagination/caching, deduplication without re-listing, batching and
its fallbacks, quota enforcement, and resumable cloning across simulated days.

Useful entry points:

```bash
python -m ytmgr --help
python -m ytmgr quota
```

---

## Project structure

```
.
├── ytmgr/
│   ├── __init__.py          Package marker
│   ├── __main__.py          Entry point for `python -m ytmgr`
│   ├── auth.py              OAuth 2.0 authentication & token cache
│   ├── cli.py               Argument parsing and command handlers
│   ├── client.py            YouTube Data API v3 wrapper
│   ├── config.py            Configuration (env vars, paths)
│   ├── errors.py            Structured API error types
│   ├── quota.py             Daily quota accounting and cost estimates
│   ├── ratelimit.py         Throttling, retry and backoff logic
│   └── tui/
│       ├── __init__.py
│       └── app.py           Interactive terminal UI (Textual)
├── tests/
│   └── test_optimisation.py Offline tests (cache/batch/quota/clone)
├── .env.example             Credentials template (safe to commit)
├── .gitignore               Ignores secrets, tokens, caches, etc.
├── requirements.txt         Python dependencies
├── setup.py                 Packaging / console-script entry point
└── README.md
```

---

## License

This project is free software licensed under the **GNU General Public License
v3.0 or later** (`GPL-3.0-or-later`). See the [LICENSE](LICENSE) file for the
full text, or read it online at
<https://www.gnu.org/licenses/gpl-3.0.html>.

GPLv3 is a strong copyleft license: if you distribute modified versions of this
software, you must make the corresponding source code available under the same
terms.
