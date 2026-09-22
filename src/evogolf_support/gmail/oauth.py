"""Google OAuth for reading the online@ mailbox.

Read-only, deliberately. The scope requested is gmail.readonly and nothing
else: this exists to learn from twelve months of support conversations, and
an ingestion job has no business being able to send, delete or label mail in
a live customer-facing inbox.

The refresh token is stored in the corpus database on the Railway volume,
the same place the Shopify token lives, so it survives a redeploy and is
never written to the repository.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import load_dotenv
from ..corpus.store import CorpusStore

log = logging.getLogger(__name__)

REFRESH_KEY = "google_refresh_token"
MAILBOX_KEY = "google_mailbox"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Read-only. Nothing here should ever be able to change the mailbox.
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"

_nonces: dict[str, float] = {}
_NONCE_TTL = 600.0


def configured() -> bool:
    load_dotenv()
    return bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip()
                and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip())


def _prune_nonces() -> None:
    now = time.time()
    for value, created in list(_nonces.items()):
        if now - created > _NONCE_TTL:
            _nonces.pop(value, None)


def new_nonce() -> str:
    _prune_nonces()
    nonce = secrets.token_urlsafe(24)
    _nonces[nonce] = time.time()
    return nonce


def consume_nonce(state: str) -> bool:
    """Check and burn a nonce. A nonce is valid exactly once."""
    _prune_nonces()
    return _nonces.pop(state, None) is not None if state else False


def install_url(redirect_uri: str, nonce: str) -> str:
    load_dotenv()
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": nonce,
        # Without both of these Google returns no refresh token on a repeat
        # authorisation, and the import silently stops working a week later
        # when the access token expires.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    load_dotenv()
    response = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=30.0)
    response.raise_for_status()
    return response.json()


def access_token(store: CorpusStore) -> str:
    """A live access token, minted from the stored refresh token."""
    load_dotenv()
    refresh = store.get_state(REFRESH_KEY)
    if not refresh:
        raise RuntimeError(
            "Gmail is not authorised yet. Open /google/install to connect the "
            "online@ mailbox."
        )
    response = httpx.post(TOKEN_URL, data={
        "refresh_token": refresh,
        "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
        "grant_type": "refresh_token",
    }, timeout=30.0)
    response.raise_for_status()
    token = response.json().get("access_token", "")
    if not token:
        raise RuntimeError("Google returned no access token.")
    return token


def store_refresh_token(store: CorpusStore, token: str) -> None:
    store.set_state(REFRESH_KEY, token)


def authorised(store: CorpusStore) -> bool:
    return bool(store.get_state(REFRESH_KEY))
