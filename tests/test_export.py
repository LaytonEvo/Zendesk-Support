"""Export logic verified against a mock Zendesk API.

Covers the two things most likely to break silently against the real API:
cursor pagination across pages, and 429 rate-limit handling.
"""

import httpx
import pytest

from evogolf_support.config import ZendeskConfig
from evogolf_support.zendesk.client import ZendeskClient, ZendeskError


def make_client(handler) -> ZendeskClient:
    config = ZendeskConfig(subdomain="test", email="a@b.c", api_token="tok")
    client = ZendeskClient.__new__(ZendeskClient)
    client.config = config
    client._client = httpx.Client(
        base_url=config.base_url, transport=httpx.MockTransport(handler)
    )
    return client


def test_incremental_export_walks_every_page():
    pages = {
        None: {
            "tickets": [{"id": 1}, {"id": 2}],
            "after_cursor": "c1",
            "end_of_stream": False,
        },
        "c1": {
            "tickets": [{"id": 3}],
            "after_cursor": "c2",
            "end_of_stream": False,
        },
        "c2": {"tickets": [{"id": 4}], "after_cursor": "c3", "end_of_stream": True},
    }
    seen_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json=pages[cursor])

    client = make_client(handler)
    ids = [t["id"] for page in client.incremental_tickets(0) for t in page["tickets"]]

    assert ids == [1, 2, 3, 4]
    # First call uses start_time, subsequent calls use the cursor.
    assert "start_time" in seen_params[0]
    assert [p.get("cursor") for p in seen_params[1:]] == ["c1", "c2"]


def test_export_stops_at_end_of_stream_even_with_a_cursor():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"tickets": [{"id": 1}], "after_cursor": "next", "end_of_stream": True},
        )

    client = make_client(handler)
    assert len(list(client.incremental_tickets(0))) == 1


def test_rate_limit_is_retried_after_waiting(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"user": {"name": "Brad"}})

    client = make_client(handler)
    assert client.verify()["name"] == "Brad"
    assert slept == [7.0]


def test_absurd_retry_after_is_clamped(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "99999"}, json={})
        return httpx.Response(200, json={"user": {}})

    client = make_client(handler)
    client.verify()
    assert slept == [300.0]


def test_bad_credentials_give_an_actionable_error():
    client = make_client(lambda r: httpx.Response(401, json={"error": "Couldn't authenticate"}))
    with pytest.raises(ZendeskError, match="ZENDESK_API_TOKEN"):
        client.verify()


def test_comments_paginate_with_cursor():
    pages = [
        {
            "comments": [{"id": 1, "body": "first"}],
            "meta": {"has_more": True, "after_cursor": "x1"},
        },
        {"comments": [{"id": 2, "body": "second"}], "meta": {"has_more": False}},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, json=page)

    client = make_client(handler)
    comments = client.ticket_comments(42)
    assert [c["id"] for c in comments] == [1, 2]
