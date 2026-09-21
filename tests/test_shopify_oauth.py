"""The install callback is the one endpoint the internet can reach unauthenticated."""

import hashlib
import hmac as hmaclib

import httpx
import pytest
from fastapi.testclient import TestClient

from evogolf_support.api import app as app_module
from evogolf_support.drafting import shopify, shopify_oauth as oauth

SECRET = "client-secret"
SHOP = "evolutiongolf.myshopify.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_EXPORT", "false")
    monkeypatch.setenv("AUTO_MINE", "false")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("ADMIN_TOKEN", "tok")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOP)
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    shopify._token_cache.update(value=None, expires_at=0.0, scopes="")
    yield


@pytest.fixture
def client(env):
    with TestClient(app_module.app) as c:
        yield c


def signed(params: dict) -> dict:
    msg = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params = dict(params)
    params["hmac"] = hmaclib.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return params


# --- shop validation ------------------------------------------------------

def test_only_well_formed_myshopify_domains_are_accepted():
    """The shop decides where we send the client secret."""
    assert oauth.valid_shop(SHOP)
    for bad in ["evil.com", "evolutiongolf.myshopify.com.evil.com", "",
                "a b.myshopify.com", "../etc/passwd", "shop.myshopify.co"]:
        assert not oauth.valid_shop(bad), bad


# --- signature ------------------------------------------------------------

def test_signature_verifies_and_tampering_is_rejected(env):
    params = signed({"code": "c", "shop": SHOP, "state": "s", "timestamp": "1"})
    assert oauth.verify_hmac(params)

    for field in ("code", "shop", "state"):
        tampered = dict(params)
        tampered[field] = "changed"
        assert not oauth.verify_hmac(tampered), field


def test_missing_or_empty_signature_is_rejected(env):
    assert not oauth.verify_hmac({"code": "c", "shop": SHOP})
    assert not oauth.verify_hmac({"code": "c", "shop": SHOP, "hmac": ""})


# --- nonce ----------------------------------------------------------------

def test_a_nonce_works_exactly_once():
    nonce = oauth.new_nonce()
    assert oauth.consume_nonce(nonce) is True
    assert oauth.consume_nonce(nonce) is False


def test_an_unknown_nonce_is_rejected():
    assert oauth.consume_nonce("never-issued") is False
    assert oauth.consume_nonce("") is False


# --- endpoints ------------------------------------------------------------

def test_install_requires_admin(client):
    assert client.get("/shopify/install", follow_redirects=False).status_code == 401


def test_install_redirects_to_shopify_with_the_right_scopes(client):
    r = client.get("/shopify/install", headers={"Authorization": "Bearer tok"},
                   follow_redirects=False)
    assert r.status_code in (302, 307)
    location = r.headers["location"]
    assert location.startswith(f"https://{SHOP}/admin/oauth/authorize")
    assert "read_all_orders" in location
    assert "client_id=cid" in location
    assert "state=" in location


def test_callback_rejects_a_wrong_shop(client):
    params = signed({"code": "c", "shop": "evil.myshopify.com", "state": "s"})
    assert client.get("/shopify/callback", params=params).status_code == 400


def test_callback_rejects_a_bad_signature(client):
    params = {"code": "c", "shop": SHOP, "state": "s", "hmac": "deadbeef"}
    assert client.get("/shopify/callback", params=params).status_code == 400


def test_callback_rejects_an_unknown_state(client):
    """A valid signature is not enough: the nonce must be one we issued."""
    params = signed({"code": "c", "shop": SHOP, "state": "never-issued"})
    assert client.get("/shopify/callback", params=params).status_code == 400


def test_callback_stores_the_token_on_success(client, monkeypatch):
    nonce = oauth.new_nonce()
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"access_token": "offline-token",
                   "scope": "read_orders,read_all_orders"},
        request=httpx.Request("POST", url)))

    params = signed({"code": "the-code", "shop": SHOP, "state": nonce})
    r = client.get("/shopify/callback", params=params)
    assert r.status_code == 200

    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore
    with CorpusStore(corpus_path()) as store:
        assert oauth.stored_token(store) == "offline-token"
    assert shopify.access_token() == "offline-token"


def test_a_replayed_callback_is_rejected(client, monkeypatch):
    """The same signed callback must not install twice."""
    nonce = oauth.new_nonce()
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"access_token": "t", "scope": "read_orders"},
        request=httpx.Request("POST", url)))

    params = signed({"code": "c", "shop": SHOP, "state": nonce})
    assert client.get("/shopify/callback", params=params).status_code == 200
    assert client.get("/shopify/callback", params=params).status_code == 400


def test_stored_token_takes_precedence_over_the_org_grant(client, monkeypatch):
    """Once installed, we must not fall back to the grant that cannot work."""
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore

    with CorpusStore(corpus_path()) as store:
        oauth.store_token(store, "installed-token")

    def boom(*a, **k):
        raise AssertionError("client credentials grant must not be attempted")

    monkeypatch.setattr(shopify, "_fetch_token", boom)
    assert shopify.access_token() == "installed-token"
    assert shopify.installed() is True
