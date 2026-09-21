"""Order lookup: reference extraction, formatting, and graceful absence."""

import httpx
import pytest

from evogolf_support.drafting import shopify


def test_order_references_found_in_real_ticket_phrasings():
    cases = {
        "Order 27641 Incorrect model ordered, please issue refund": ["27641"],
        "refund for order #29103 please": ["29103"],
        "Your order (WEB-UK31018) is on the way": ["WEB-UK31018"],
        "query on invoice INV1133418": ["INV1133418"],
    }
    for text, expected in cases.items():
        assert shopify.order_references(text) == expected, text


def test_no_false_positives_without_an_order_reference():
    """Quantities and years must not be read as order numbers."""
    for text in ["I bought 3 balls and 2 gloves last week",
                 "my handicap went from 18 to 12 in 2025",
                 "Hi, can you help with sizing?"]:
        assert shopify.order_references(text) == [], text


def test_lookup_is_skipped_when_not_configured(monkeypatch):
    """No credentials means no order context - never a crash."""
    monkeypatch.delenv("SHOPIFY_STORE_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    assert shopify.configured() is False
    assert shopify.context_for_ticket("order 27641 where is it") == ""
    assert shopify.find_orders(reference="27641") == []


def test_api_failure_degrades_to_no_context(monkeypatch):
    """A Shopify outage must cost us the figures, not the draft."""
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "evolutiongolf.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(shopify, "_post",
                        lambda q, v: (_ for _ in ()).throw(RuntimeError("503")))
    assert shopify.find_orders(reference="27641") == []
    assert shopify.context_for_ticket("order 27641") == ""


def test_format_renders_the_facts_a_draft_needs():
    orders = [{
        "name": "#29103", "createdAt": "2026-09-02T10:00:00Z", "cancelledAt": None,
        "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "FULFILLED",
        "currentTotalPriceSet": {"shopMoney": {"amount": "429.00", "currencyCode": "GBP"}},
        "customer": {"firstName": "Jayman", "lastName": "Patel"},
        "lineItems": {"nodes": [
            {"title": "Motocaddy M7 GPS", "quantity": 1, "variantTitle": "Black", "sku": "M7"}]},
        "fulfillments": [{"createdAt": "2026-09-03T09:00:00Z", "status": "SUCCESS",
                          "trackingInfo": [{"company": "DPD", "number": "6978946422", "url": "x"}]}],
        "refunds": [], "returns": {"nodes": []},
    }]
    out = shopify.format_for_prompt(orders)
    assert "#29103" in out and "Jayman Patel" in out
    assert "GBP 429.00" in out
    assert "1x Motocaddy M7 GPS (Black)" in out
    assert "DPD tracking 6978946422" in out
    assert "FULFILLED" in out


def test_format_flags_a_fulfilment_with_no_tracking():
    orders = [{
        "name": "#1", "createdAt": "x", "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "FULFILLED", "currentTotalPriceSet": None,
        "customer": None, "lineItems": {"nodes": []},
        "fulfillments": [{"createdAt": "y", "status": "SUCCESS", "trackingInfo": []}],
        "refunds": [], "returns": {"nodes": []},
    }]
    assert "no tracking recorded" in shopify.format_for_prompt(orders)


def test_graphql_errors_raise_rather_than_returning_bad_data(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "x.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "tok")

    # A Response needs its request set before raise_for_status() can run.
    def fake_post(url, **kwargs):
        return httpx.Response(
            200,
            json={"errors": [{"message": "Access denied"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="Access denied"):
        shopify._post("query{x}", {})


def test_empty_orders_render_as_empty_string():
    assert shopify.format_for_prompt([]) == ""


# --- client credentials grant ---------------------------------------------

@pytest.fixture(autouse=True)
def _clear_token_cache():
    shopify._token_cache.update(value=None, expires_at=0.0, scopes="")
    yield
    shopify._token_cache.update(value=None, expires_at=0.0, scopes="")


def _creds(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "evolutiongolf.myshopify.com")
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "secret")


def test_configured_accepts_client_credentials(monkeypatch):
    _creds(monkeypatch)
    assert shopify.configured() is True


def test_configured_still_accepts_a_legacy_token(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "x.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_old")
    monkeypatch.delenv("SHOPIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SHOPIFY_CLIENT_SECRET", raising=False)
    assert shopify.configured() is True
    assert shopify.access_token() == "shpat_old"


def test_credentials_are_exchanged_for_a_token(monkeypatch):
    _creds(monkeypatch)
    sent = {}

    def fake_post(url, **kw):
        sent["url"] = url
        sent["data"] = kw.get("data")
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 86399,
                                         "scope": "read_orders,read_all_orders"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert shopify.access_token() == "tok-1"
    assert sent["url"].endswith("/admin/oauth/access_token")
    assert sent["data"]["grant_type"] == "client_credentials"
    assert sent["data"]["client_id"] == "cid"
    assert sent["data"]["client_secret"] == "secret"


def test_token_is_cached_and_not_refetched_every_call(monkeypatch):
    _creds(monkeypatch)
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"tok-{calls['n']}",
                                         "expires_in": 86399, "scope": "read_orders"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert shopify.access_token() == "tok-1"
    assert shopify.access_token() == "tok-1"
    assert calls["n"] == 1


def test_expired_token_is_refreshed(monkeypatch):
    """These tokens last 24 hours, unlike the permanent shpat_ ones."""
    _creds(monkeypatch)
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"tok-{calls['n']}",
                                         "expires_in": 86399, "scope": "read_orders"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert shopify.access_token() == "tok-1"
    shopify._token_cache["expires_at"] = 0.0      # simulate expiry
    assert shopify.access_token() == "tok-2"
    assert calls["n"] == 2


def test_shop_not_permitted_gives_an_actionable_error(monkeypatch):
    """The most likely setup failure: app and store in different orgs."""
    _creds(monkeypatch)

    def fake_post(url, **kw):
        return httpx.Response(
            401, text='{"error":"Oauth error shop_not_permitted: Client credentials '
                      'cannot be performed on this shop."}',
            request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="same Shopify organization"):
        shopify.access_token()


def test_missing_read_all_orders_is_warned_about(monkeypatch, caplog):
    """Without it, orders older than 60 days return nothing and no error."""
    import logging

    _creds(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"access_token": "t", "expires_in": 86399, "scope": "read_orders"},
        request=httpx.Request("POST", url)))

    with caplog.at_level(logging.WARNING):
        shopify.access_token()
    assert any("read_all_orders" in r.message for r in caplog.records)


def test_no_warning_when_read_all_orders_is_granted(monkeypatch, caplog):
    import logging

    _creds(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"access_token": "t", "expires_in": 86399,
                   "scope": "read_orders,read_all_orders,read_customers"},
        request=httpx.Request("POST", url)))

    with caplog.at_level(logging.WARNING):
        shopify.access_token()
    assert not any("read_all_orders is NOT granted" in r.message for r in caplog.records)
    assert "read_all_orders" in shopify.granted_scopes()
