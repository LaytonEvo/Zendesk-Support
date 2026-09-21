"""OAuth install flow for a live merchant store.

The client credentials grant only reaches stores inside the app's own Shopify
organization. evolutiongolf.co.uk is a live merchant store, so the app is
distributed to it privately and installed once; Shopify then returns a
long-lived offline token.

The callback cannot carry our own admin token - Shopify calls it - so its only
protections are the HMAC signature and the single-use nonce. Both are enforced
strictly, and the shop parameter is validated rather than trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import urllib.parse
from typing import Any

import httpx

from ..config import load_dotenv
from ..corpus.store import CorpusStore

log = logging.getLogger(__name__)

TOKEN_KEY = "shopify_offline_token"
SCOPES = "read_orders,read_all_orders,read_customers,read_returns"

# A shop parameter is attacker-controlled: it decides which host we send the
# client secret to. Only a well-formed myshopify.com subdomain is accepted.
_SHOP = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$", re.IGNORECASE)

# Nonces live in memory: single-use, short-lived, and a restart simply means
# starting the install again.
_NONCE_TTL = 600.0
_nonces: dict[str, float] = {}


def valid_shop(shop: str) -> bool:
    return bool(shop and _SHOP.match(shop.strip()))


def expected_shop() -> str:
    load_dotenv()
    return os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip().lower()


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


def install_url(shop: str, redirect_uri: str, nonce: str) -> str:
    load_dotenv()
    params = {
        "client_id": os.environ["SHOPIFY_CLIENT_ID"].strip(),
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": nonce,
    }
    return f"https://{shop}/admin/oauth/authorize?{urllib.parse.urlencode(params)}"


def verify_hmac(params: dict[str, Any]) -> bool:
    """Verify Shopify's signature over the callback query parameters.

    Per Shopify: remove hmac, sort the rest alphabetically, HMAC-SHA256 with
    the client secret, and compare in constant time.
    """
    load_dotenv()
    secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    provided = params.get("hmac")
    if not secret or not provided:
        return False

    rest = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
    message = "&".join(f"{k}={rest[k]}" for k in sorted(rest))
    digest = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, str(provided))


def exchange_code(shop: str, code: str) -> dict[str, Any]:
    load_dotenv()
    response = httpx.post(
        f"https://{shop}/admin/oauth/access_token",
        data={
            "client_id": os.environ["SHOPIFY_CLIENT_ID"].strip(),
            "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"].strip(),
            "code": code,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Shopify code exchange failed ({response.status_code}): "
            f"{response.text[:300]}"
        )
    payload = response.json()
    if not payload.get("access_token"):
        raise RuntimeError("Shopify code exchange returned no access_token")
    return payload


def store_token(store: CorpusStore, token: str) -> None:
    store.set_state(TOKEN_KEY, token)


def stored_token(store: CorpusStore) -> str | None:
    return store.get_state(TOKEN_KEY)
