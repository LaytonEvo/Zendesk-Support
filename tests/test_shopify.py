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
