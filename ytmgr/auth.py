"""Authentication module for YouTube Data API v3."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ytmgr.config import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPES,
    TOKEN_DIR,
    TOKEN_FILE,
)


def _make_token_dir() -> None:
    """Ensure the token directory exists."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)


def _credentials_from_dict(data: dict) -> Credentials:
    """Reconstruct Credentials from a stored dictionary."""
    expiry = data.get("expiry")
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id", CLIENT_ID),
        client_secret=data.get("client_secret", CLIENT_SECRET),
        scopes=data.get("scopes", SCOPES),
        expiry=expiry,
    )


def _credentials_to_dict(creds: Credentials) -> dict:
    """Serialize Credentials to a JSON-safe dictionary."""
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def _load_token() -> Optional[Credentials]:
    """Load credentials from the local token file, if it exists and is valid."""
    if not TOKEN_FILE.exists():
        return None

    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        creds = _credentials_from_dict(data)

        # Try refreshing if expired, then persist the updated token
        if creds and creds.expired:
            if creds.refresh_token:
                request = Request()
                creds.refresh(request)
                _save_token(creds)

        return creds
    except Exception:
        return None


def _save_token(creds: Credentials) -> None:
    """Persist credentials to the local token file."""
    _make_token_dir()
    data = _credentials_to_dict(creds)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)
    # Restrict permissions on Unix-like systems
    try:
        TOKEN_FILE.chmod(0o600)
    except Exception:
        pass


def authenticate(force: bool = False) -> Credentials:
    """
    Obtain valid credentials, either from a cached token or by running the
    OAuth 2.0 browser flow.

    Args:
        force: If True, ignore any cached token and re-authenticate.

    Returns:
        A valid google.oauth2.credentials.Credentials instance.
    """
    if not force:
        creds = _load_token()
        if creds and creds.valid:
            return creds

    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Missing CLIENT_ID or CLIENT_SECRET. "
            "Create a .env file based on .env.example with your "
            "YouTube Data API v3 OAuth 2.0 credentials."
        )

    flow = InstalledAppFlow.from_client_config(
        client_config={
            "installed": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )

    # Run local server to handle the OAuth callback on localhost
    creds = flow.run_local_server(port=0, open_browser=True)

    _save_token(creds)
    return creds


def revoke_token() -> None:
    """Remove the stored token file (does not revoke server-side)."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("Token removed.")
    else:
        print("No token found.")


def get_authenticated_service():
    """
    Build and return an authenticated youtube v3 service object.

    Returns:
        googleapiclient.discovery.Resource
    """
    from googleapiclient.discovery import build

    creds = authenticate()
    return build("youtube", "v3", credentials=creds)