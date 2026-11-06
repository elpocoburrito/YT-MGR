"""Configuration loader for environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# OAuth 2.0 credentials
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")

# OAuth scopes - full access to manage YouTube playlists
SCOPES = ["https://www.googleapis.com/auth/youtube"]

# Token storage path (relative to user's home directory)
TOKEN_DIR = Path.home() / ".ytmgr"
TOKEN_FILE = TOKEN_DIR / "token.json"

# API service name
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

# ---------------------------------------------------------------------------
# Throttling / retry settings
# ---------------------------------------------------------------------------
# Minimum number of seconds between API requests. 0.25s ≈ 4 requests/sec.
# Increase this (e.g. 1.0) if you keep hitting rate limits.
MIN_REQUEST_INTERVAL = _env_float("YTMGR_MIN_INTERVAL", 0.25)

# Maximum retry attempts for transient errors (rate limits, 5xx, network).
MAX_RETRIES = _env_int("YTMGR_MAX_RETRIES", 5)

# Exponential backoff base delay (seconds) and cap (seconds).
BACKOFF_BASE = _env_float("YTMGR_BACKOFF_BASE", 1.0)
BACKOFF_MAX = _env_float("YTMGR_BACKOFF_MAX", 60.0)

# ---------------------------------------------------------------------------
# Quota accounting settings
# ---------------------------------------------------------------------------
# YouTube Data API default daily quota (units). Override for a different project.
DAILY_QUOTA_LIMIT = _env_int("YTMGR_DAILY_QUOTA", 10000)

# Warn when this fraction of the daily quota has been consumed (0.0 - 1.0).
QUOTA_WARN_RATIO = _env_float("YTMGR_QUOTA_WARN_RATIO", 0.8)

# When enabled, refuse to issue a request that would exceed the daily quota
# (prevents server-side 403 quotaExceeded errors). Set to 0 to disable.
QUOTA_ENFORCE = os.getenv("YTMGR_QUOTA_ENFORCE", "1").lower() not in (
    "0",
    "false",
    "no",
)

# Persisted quota usage (per day) and clone checkpoints live under ~/.ytmgr.
QUOTA_FILE = TOKEN_DIR / "quota.json"
CLONE_DIR = TOKEN_DIR / "clones"

# --------------------------------------------------------------------------
# Batching settings (reduces HTTP requests for bulk inserts)
# --------------------------------------------------------------------------
# Number of sub-requests per batch (YouTube allows up to 50).
BATCH_SIZE = max(1, min(50, _env_int("YTMGR_BATCH_SIZE", 50)))

# Use batched requests for bulk operations when supported. Set to 0 to disable.
USE_BATCH = os.getenv("YTMGR_USE_BATCH", "1").lower() not in (
    "0",
    "false",
    "no",
)
