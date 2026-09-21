"""The delay sweep contacts customers who have not contacted us."""

import datetime as dt

import pytest

from evogolf_support.corpus.store import CorpusStore
from evogolf_support.proactive import detect, notify, run
from evogolf_support.proactive.detect import AtRisk, working_days_between

UTC = dt.timezone.utc
MON = dt.datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


def _order(name, created, fulfilment="UNFULFILLED", fulfillments=None):
    return {
        "id": f"gid://shopify/Order/{name}", "name": name,
        "createdAt": created.isoformat().replace("+00:00", "Z"),
        "email": "c@example.com", "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": fulfilment,
        "customer": {"firstName": "Craig", "lastName": "Whitfield"},
        "currentTotalPriceSet": {"shopMoney": {"amount": "429", "currencyCode": "GBP"}},
        "lineItems": {"nodes": [{"title": "Motocaddy M7", "quantity": 1}]},
        "fulfillments": fulfillments or [],
    }


def _run_detect(monkeypatch, orders, now):
    monkeypatch.setattr(detect.shopify, "configured", lambda: True)
    monkeypatch.setattr(detect.shopify, "_post",
                        lambda q, v: {"orders": {"nodes": orders}})
    return detect.find_at_risk(now=now)


# --- working days ---------------------------------------------------------

def test_weekends_do_not_count_towards_lateness():
    assert working_days_between(MON, MON + dt.timedelta(days=4)) == 4   # Fri
    assert working_days_between(MON, MON + dt.timedelta(days=6)) == 4   # Sun
    assert working_days_between(MON, MON + dt.timedelta(days=7)) == 5   # next Mon
    assert working_days_between(MON, MON) == 0
    assert working_days_between(MON, MON - dt.timedelta(days=3)) == 0


# --- detection ------------------------------------------------------------

def test_recent_unfulfilled_order_is_not_flagged(monkeypatch):
    """Two working days old is not late."""
    now = MON + dt.timedelta(days=2)
    assert _run_detect(monkeypatch, [_order("#1", MON)], now) == []


def test_unfulfilled_beyond_three_working_days_is_flagged(monkeypatch):
    now = MON + dt.timedelta(days=3)
    found = _run_detect(monkeypatch, [_order("#1", MON)], now)
    assert [f.reason for f in found] == ["not_dispatched"]
    assert found[0].days == 3


def test_delivered_orders_are_never_flagged(monkeypatch):
    now = MON + dt.timedelta(days=20)
    orders = [_order("#1", MON, "FULFILLED", [
        {"createdAt": MON.isoformat().replace("+00:00", "Z"),
         "displayStatus": "DELIVERED", "trackingInfo": []}])]
    assert _run_detect(monkeypatch, orders, now) == []


def test_in_transit_beyond_five_working_days_is_flagged(monkeypatch):
    """Needs a delivered order in the window to prove the courier feed works."""
    now = MON + dt.timedelta(days=9)   # 7 working days
    orders = [
        _order("#0", MON, "FULFILLED", [
            {"createdAt": MON.isoformat().replace("+00:00", "Z"),
             "displayStatus": "DELIVERED", "trackingInfo": []}]),
        _order("#1", MON, "FULFILLED", [
            {"createdAt": MON.isoformat().replace("+00:00", "Z"),
             "displayStatus": "IN_TRANSIT",
             "trackingInfo": [{"company": "DPD", "number": "123"}]}]),
    ]
    found = _run_detect(monkeypatch, orders, now)
    assert [f.reason for f in found] == ["stuck_in_transit"]
    assert found[0].order_name == "#1"
    assert found[0].tracking == "DPD 123"


def test_in_transit_within_the_window_is_left_alone(monkeypatch):
    now = MON + dt.timedelta(days=4)
    orders = [_order("#1", MON, "FULFILLED", [
        {"createdAt": MON.isoformat().replace("+00:00", "Z"),
         "displayStatus": "IN_TRANSIT", "trackingInfo": []}])]
    assert _run_detect(monkeypatch, orders, now) == []


def test_courier_failure_is_flagged_immediately(monkeypatch):
    """The customer usually knows before we do, so elapsed time is irrelevant."""
    now = MON + dt.timedelta(days=1)
    for status in ("DELAYED", "ATTEMPTED_DELIVERY", "NOT_DELIVERED"):
        orders = [_order("#1", MON, "FULFILLED", [
            {"createdAt": MON.isoformat().replace("+00:00", "Z"),
             "displayStatus": status, "trackingInfo": []}])]
        found = _run_detect(monkeypatch, orders, now)
        assert [f.reason for f in found] == ["delivery_problem"], status


def test_no_shopify_means_no_sweep(monkeypatch):
    monkeypatch.setattr(detect.shopify, "configured", lambda: False)
    assert detect.find_at_risk() == []


# --- ticket safety --------------------------------------------------------

def _item():
    return AtRisk(order_name="#29457", order_id="gid://1", reason="not_dispatched",
                  detail="Paid 4 working days ago and still not dispatched", days=4,
                  email="craig@example.com", customer_name="Craig Whitfield",
                  items="1x Motocaddy M7", tracking="")


def _draft(hand_over=False):
    from evogolf_support.drafting.generate import Draft
    return Draft(hand_to_agent=hand_over, handover_reason="needs a call" if hand_over else "",
                 draft="Hi Craig,\n\nYour order is running late.", confidence="high",
                 rules_applied=[9], tickets_referenced=[], agent_notes=["Confirm the cause"])


def test_the_draft_is_an_internal_note_never_a_public_reply(monkeypatch):
    """The whole point is review before send."""
    sent = {}

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def create_ticket(self, ticket): sent.update(ticket); return {"id": 5}

    monkeypatch.setattr(notify, "ZendeskClient", lambda *a, **k: FakeClient())
    notify.raise_ticket(_item(), _draft())

    assert sent["comment"]["public"] is False
    assert "nothing has been sent to the customer" in sent["comment"]["body"].lower()
    assert "Confirm the cause" in sent["comment"]["body"]


def test_no_requester_is_set_by_default(monkeypatch):
    """A requester can fire the account's own triggers and email the customer."""
    sent = {}

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def create_ticket(self, ticket): sent.update(ticket); return {"id": 5}

    monkeypatch.delenv("PROACTIVE_SET_REQUESTER", raising=False)
    monkeypatch.setattr(notify, "ZendeskClient", lambda *a, **k: FakeClient())
    notify.raise_ticket(_item(), _draft())
    assert "requester" not in sent


def test_requester_is_set_only_when_explicitly_enabled(monkeypatch):
    sent = {}

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def create_ticket(self, ticket): sent.update(ticket); return {"id": 5}

    monkeypatch.setenv("PROACTIVE_SET_REQUESTER", "true")
    monkeypatch.setattr(notify, "ZendeskClient", lambda *a, **k: FakeClient())
    notify.raise_ticket(_item(), _draft())
    assert sent["requester"]["email"] == "craig@example.com"


def test_a_handover_is_stated_on_the_ticket(monkeypatch):
    sent = {}

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def create_ticket(self, ticket): sent.update(ticket); return {"id": 5}

    monkeypatch.setattr(notify, "ZendeskClient", lambda *a, **k: FakeClient())
    notify.raise_ticket(_item(), _draft(hand_over=True))
    assert "HANDED OVER" in sent["comment"]["body"]


# --- sweep ----------------------------------------------------------------

def test_a_customer_is_only_contacted_once_per_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setattr(run, "find_at_risk", lambda: [_item()])
    monkeypatch.setattr(run, "draft_for", lambda store, item: _draft())
    monkeypatch.setattr(run, "raise_ticket", lambda item, draft: 77)
    monkeypatch.setattr(run, "send_digest", lambda lines: True)

    first = run.run_sweep()
    assert first["tickets"] == 1 and first["new"] == 1

    second = run.run_sweep()
    assert second["tickets"] == 0 and second["skipped_seen"] == 1


def test_a_mass_delay_raises_no_tickets_and_asks_for_a_human(tmp_path, monkeypatch):
    """Forty late orders is one upstream problem, not forty conversations."""
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    many = []
    for i in range(run.MAX_PER_SWEEP + 5):
        item = _item()
        item.order_name = f"#{i}"
        many.append(item)
    digest = {}
    monkeypatch.setattr(run, "find_at_risk", lambda: many)
    monkeypatch.setattr(run, "raise_ticket",
                        lambda item, draft: pytest.fail("must not raise tickets"))
    monkeypatch.setattr(run, "send_digest", lambda lines: digest.update(lines=lines))

    result = run.run_sweep()
    assert result["tickets"] == 0
    assert "above the automatic ceiling" in digest["lines"][0]


def test_a_failed_ticket_is_retried_next_sweep(tmp_path, monkeypatch):
    """Only orders that reached a ticket are marked as done."""
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setattr(run, "find_at_risk", lambda: [_item()])
    monkeypatch.setattr(run, "draft_for", lambda store, item: _draft())
    monkeypatch.setattr(run, "send_digest", lambda lines: True)
    monkeypatch.setattr(run, "raise_ticket", lambda item, draft: None)   # failed

    assert run.run_sweep()["tickets"] == 0
    monkeypatch.setattr(run, "raise_ticket", lambda item, draft: 88)
    assert run.run_sweep()["tickets"] == 1


def test_dry_run_raises_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setattr(run, "find_at_risk", lambda: [_item()])
    monkeypatch.setattr(run, "raise_ticket",
                        lambda item, draft: pytest.fail("dry run must not raise"))
    monkeypatch.setattr(run, "send_digest", lambda lines: True)
    assert run.run_sweep(dry_run=True)["tickets"] == 0


# --- the browser-openable preview ----------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from evogolf_support.api import app as app_module

    monkeypatch.setenv("AUTO_EXPORT", "false")
    monkeypatch.setenv("AUTO_MINE", "false")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("ADMIN_TOKEN", "tok")
    with TestClient(app_module.app) as c:
        yield c


def test_preview_opens_with_a_token_in_the_url(client, monkeypatch):
    monkeypatch.setattr(detect, "find_at_risk", lambda: [_item()])
    r = client.get("/proactive/preview", params={"token": "tok"})
    assert r.status_code == 200
    assert "#29457" in r.text and "Craig Whitfield" in r.text
    assert "no customer has been contacted" in r.text


def test_preview_rejects_a_wrong_token(client):
    assert client.get("/proactive/preview", params={"token": "no"}).status_code == 401
    assert client.get("/proactive/preview").status_code == 401


def test_preview_says_so_when_nothing_is_late(client, monkeypatch):
    monkeypatch.setattr(detect, "find_at_risk", lambda: [])
    r = client.get("/proactive/preview", params={"token": "tok"})
    assert "Nothing is running late" in r.text


def test_preview_escapes_order_and_customer_data(client, monkeypatch):
    """Order and customer text comes from Shopify and lands in a page."""
    item = _item()
    item.customer_name = '<script>alert(1)</script>'
    monkeypatch.setattr(detect, "find_at_risk", lambda: [item])
    r = client.get("/proactive/preview", params={"token": "tok"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_the_browser_reachable_endpoint_can_never_raise_a_ticket(client, monkeypatch):
    """A GET can be re-requested by a prefetcher or a bookmark."""
    monkeypatch.setattr(detect, "find_at_risk", lambda: [_item()])
    monkeypatch.setattr(run, "raise_ticket",
                        lambda item, draft: pytest.fail("preview must not raise tickets"))
    assert client.get("/proactive/preview", params={"token": "tok"}).status_code == 200


def test_the_real_sweep_still_requires_a_post_and_a_header(client):
    assert client.get("/proactive/sweep", params={"token": "tok"}).status_code == 405
    assert client.post("/proactive/sweep").status_code == 401


# --- the 61-false-positive regression ------------------------------------

def _fulfilled(name, created, status):
    return _order(name, created, "FULFILLED", [
        {"createdAt": created.isoformat().replace("+00:00", "Z"),
         "displayStatus": status, "trackingInfo": []}])


def test_dispatched_is_not_in_transit(monkeypatch):
    """FULFILLED means 'we handed it over', not 'the courier has it'.

    Regression: a real preview flagged 61 orders as stuck in transit at 17-31
    working days - the shop's entire shipping book. Their courier never feeds
    status back, so every fulfilment sat at FULFILLED forever and eventually
    tripped the clock.
    """
    now = MON + dt.timedelta(days=45)
    orders = [_fulfilled(f"#{i}", MON, "FULFILLED") for i in range(60)]
    assert _run_detect(monkeypatch, orders, now) == []


def test_merchant_side_statuses_are_never_treated_as_transit(monkeypatch):
    now = MON + dt.timedelta(days=45)
    for status in ("SUBMITTED", "CONFIRMED", "LABEL_PRINTED",
                   "LABEL_PURCHASED", "MARKED_AS_FULFILLED"):
        orders = [_fulfilled("#1", MON, status)]
        assert _run_detect(monkeypatch, orders, now) == [], status


def test_in_transit_is_used_when_the_courier_feed_demonstrably_works(monkeypatch):
    """One delivered order in the window proves the feed reports back."""
    now = MON + dt.timedelta(days=45)
    orders = [_fulfilled(f"#{i}", MON, "DELIVERED") for i in range(5)]
    orders.append(_fulfilled("#99", MON, "IN_TRANSIT"))
    found = _run_detect(monkeypatch, orders, now)
    assert [f.order_name for f in found] == ["#99"]


def test_in_transit_is_ignored_when_nothing_ever_reaches_delivered(monkeypatch):
    """Without a terminal status anywhere, 'in transit' carries no meaning."""
    now = MON + dt.timedelta(days=45)
    orders = [_fulfilled(f"#{i}", MON, "IN_TRANSIT") for i in range(30)]
    assert _run_detect(monkeypatch, orders, now) == []


def test_courier_failures_are_still_caught_without_a_working_feed(monkeypatch):
    """An explicit failure is the courier telling us something, feed or not."""
    now = MON + dt.timedelta(days=45)
    orders = [_fulfilled(f"#{i}", MON, "FULFILLED") for i in range(20)]
    orders.append(_fulfilled("#7", MON, "ATTEMPTED_DELIVERY"))
    found = _run_detect(monkeypatch, orders, now)
    assert [(f.order_name, f.reason) for f in found] == [("#7", "delivery_problem")]


def test_undispatched_orders_are_unaffected_by_the_feed_question(monkeypatch):
    now = MON + dt.timedelta(days=45)
    orders = [_fulfilled(f"#{i}", MON, "FULFILLED") for i in range(20)]
    orders.append(_order("#5", MON))          # paid, never dispatched
    found = _run_detect(monkeypatch, orders, now)
    assert [(f.order_name, f.reason) for f in found] == [("#5", "not_dispatched")]
