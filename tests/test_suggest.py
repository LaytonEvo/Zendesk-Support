"""Drafts delivered as an internal note, for plans that cannot upload an app.

The loop guards carry most of the weight here. The webhook writes back to
the ticket that fired it, so every test that stops it writing twice is
stopping an unbounded loop against live customer tickets.
"""

import datetime as dt

import pytest

from evogolf_support.corpus.store import CorpusStore
from evogolf_support.drafting.generate import Draft
from evogolf_support.zendesk import suggest

AGENT_ID, CUSTOMER_ID, API_USER_ID = 11, 22, 99


def _draft(**kw):
    base = dict(hand_to_agent=False, handover_reason="", draft="Thanks for getting "
                "in touch. Your order is on its way.\n\nSupport Team",
                confidence="high", rules_applied=[2, 16], tickets_referenced=[1234],
                agent_notes=["Confirm the tracking number."])
    base.update(kw)
    return Draft(**base)


def _comment(cid, author, public=True, body="My order has not arrived."):
    return {"id": cid, "author_id": author, "public": public,
            "body": body, "plain_body": body}


class FakeClient:
    """Stands in for Zendesk. Records what would have been written."""

    def __init__(self, comments, status="open", requester=CUSTOMER_ID):
        self.comments = comments
        self.status = status
        self.requester = requester
        self.notes: list[tuple[int, str]] = []
        self.public_writes: list[dict] = []

    def __enter__(self): return self
    def __exit__(self, *a): return None

    def ticket(self, ticket_id):
        return {"id": ticket_id, "subject": "Where is my order?",
                "status": self.status, "requester_id": self.requester,
                "via": {"source": {"from": {"address": "c@example.com"}}}}

    def ticket_comments(self, ticket_id):
        return list(self.comments)

    def verify(self):
        return {"id": API_USER_ID}

    def add_internal_note(self, ticket_id, body):
        self.notes.append((ticket_id, body))
        return {"id": ticket_id}

    def put(self, path, payload):
        comment = (payload.get("ticket") or {}).get("comment") or {}
        if comment.get("public") is not False:
            self.public_writes.append(comment)
        return {"ticket": {}}


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "corpus.sqlite3"
    with CorpusStore(path):
        pass
    return path


@pytest.fixture
def wired(monkeypatch):
    """Patch out everything beyond the logic under test."""
    suggest._api_user_id = None
    monkeypatch.setattr(suggest.shopify, "context_for_ticket",
                        lambda *a, **k: "Order #1234, dispatched.")
    made: list[dict] = []

    def fake_draft(store, **kw):
        made.append(kw)
        return _draft()

    monkeypatch.setattr(suggest, "draft_reply", fake_draft)
    return made


def _install(monkeypatch, client):
    monkeypatch.setattr(suggest, "ZendeskClient", lambda *a, **k: client)
    return client


# --- the happy path -------------------------------------------------------

def test_a_customer_message_gets_a_note(monkeypatch, corpus, wired):
    client = _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    assert suggest.suggest_for_ticket(5, corpus) == "suggested"
    assert len(client.notes) == 1
    ticket_id, body = client.notes[0]
    assert ticket_id == 5
    assert "Suggested reply" in body and "Support Team" in body
    assert "Confirm the tracking number." in body


def test_the_customer_message_and_order_reach_the_draft(monkeypatch, corpus, wired):
    _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    suggest.suggest_for_ticket(5, corpus)
    assert wired[0]["subject"] == "Where is my order?"
    assert wired[0]["body"] == "My order has not arrived."
    assert wired[0]["order_context"] == "Order #1234, dispatched."


# --- the loop guards ------------------------------------------------------

def test_our_own_note_does_not_produce_another(monkeypatch, corpus, wired):
    """Without this the webhook answers itself, forever.

    Read from the thread rather than from stored state, so it still holds if
    the corpus database is ever rebuilt from scratch.
    """
    client = _install(monkeypatch, FakeClient(
        [_comment(1, CUSTOMER_ID), _comment(2, API_USER_ID, public=False)]))
    outcome = suggest.suggest_for_ticket(5, corpus)
    assert client.notes == []
    assert outcome == "already noted on this message"


def test_a_public_comment_from_us_does_not_produce_a_note(monkeypatch, corpus, wired):
    """Belt and braces: the private test alone would not catch a public reply."""
    client = _install(monkeypatch, FakeClient(
        [_comment(1, CUSTOMER_ID), _comment(2, API_USER_ID, public=True)]))
    suggest.suggest_for_ticket(5, corpus)
    assert client.notes == []


def test_an_auto_acknowledgement_does_not_suppress_the_draft(monkeypatch, corpus, wired):
    """This account auto-acknowledges every new email ticket within seconds.

    That reply is public and agent-authored, so by the time this handler
    reads the thread the customer's message is no longer last. Requiring the
    last comment to be the customer's would have skipped every new email
    ticket - silently, with a log line that read like a correct decision.
    """
    client = _install(monkeypatch, FakeClient([
        _comment(1, CUSTOMER_ID),
        _comment(2, AGENT_ID, body="Thanks, we have received your message."),
    ]))
    assert suggest.suggest_for_ticket(5, corpus) == "suggested"
    assert len(client.notes) == 1


def test_the_draft_answers_the_customer_not_the_acknowledgement(monkeypatch, corpus, wired):
    client = _install(monkeypatch, FakeClient([
        _comment(1, CUSTOMER_ID, body="Where is my trolley?"),
        _comment(2, AGENT_ID, body="Thanks, we have received your message."),
    ]))
    suggest.suggest_for_ticket(5, corpus)
    assert wired[0]["body"] == "Where is my trolley?"


def test_an_internal_note_from_an_agent_is_not_replied_to(monkeypatch, corpus, wired):
    """A colleague's private aside is not a customer message."""
    client = _install(monkeypatch, FakeClient(
        [_comment(1, AGENT_ID, public=False, body="Chasing DPD on this one.")]))
    assert suggest.suggest_for_ticket(5, corpus) == "nothing from the requester to reply to"
    assert client.notes == []


def test_the_same_comment_is_never_drafted_twice(monkeypatch, corpus, wired):
    """Zendesk retries webhooks; a retry must not mean a second note."""
    client = _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    assert suggest.suggest_for_ticket(5, corpus) == "suggested"
    assert suggest.suggest_for_ticket(5, corpus) == "already suggested for this comment"
    assert len(client.notes) == 1


def test_a_new_customer_message_is_drafted_again(monkeypatch, corpus, wired):
    client = _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    suggest.suggest_for_ticket(5, corpus)
    client.comments.append(_comment(2, CUSTOMER_ID, body="Any update?"))
    assert suggest.suggest_for_ticket(5, corpus) == "suggested"
    assert len(client.notes) == 2


def test_closed_tickets_are_left_alone(monkeypatch, corpus, wired):
    for state in ("closed", "solved"):
        client = _install(monkeypatch, FakeClient(
            [_comment(1, CUSTOMER_ID)], status=state))
        assert suggest.suggest_for_ticket(5, corpus) == "ticket already closed"
        assert client.notes == []


def test_a_runaway_trigger_hits_a_ceiling(monkeypatch, corpus, wired):
    """A misconfigured trigger should cost forty drafts, not a thousand."""
    monkeypatch.setattr(suggest, "HOURLY_CEILING", 3)
    seen = []
    for i in range(6):
        client = _install(monkeypatch, FakeClient([_comment(i, CUSTOMER_ID)]))
        seen.append(suggest.suggest_for_ticket(100 + i, corpus))
    assert seen.count("suggested") == 3
    assert seen.count("hourly ceiling reached") == 3


def test_the_ceiling_resets_with_the_hour(monkeypatch, corpus, wired):
    monkeypatch.setattr(suggest, "HOURLY_CEILING", 1)
    _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    assert suggest.suggest_for_ticket(5, corpus) == "suggested"

    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(suggest.dt, "datetime", Clock)
    _install(monkeypatch, FakeClient([_comment(9, CUSTOMER_ID)]))
    assert suggest.suggest_for_ticket(6, corpus) == "suggested"


# --- what lands in the note ----------------------------------------------

def test_the_note_is_private():
    """The one flag between an internal note and an unreviewed customer reply."""
    from evogolf_support.zendesk.client import ZendeskClient
    import inspect
    source = inspect.getsource(ZendeskClient.add_internal_note)
    assert '"public": False' in source


def test_nothing_is_ever_written_publicly(monkeypatch, corpus, wired):
    client = _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    suggest.suggest_for_ticket(5, corpus)
    assert client.public_writes == []


def test_a_handover_withholds_the_draft():
    """A reply sitting in the thread gets used. That is the point of a handover."""
    note = suggest.format_note(_draft(
        hand_to_agent=True, handover_reason="Policy rule 3: refunds need a person.",
        draft="Here is your refund, no questions asked."))
    assert "Needs an agent" in note
    assert "Policy rule 3" in note
    assert "refund, no questions asked" not in note


def test_the_note_says_it_has_not_been_sent(monkeypatch, corpus, wired):
    """An agent must never mistake a draft for something the customer has seen."""
    client = _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    suggest.suggest_for_ticket(5, corpus)
    body = client.notes[0][1]
    assert "not sent to the customer" in body
    assert "Nothing here has reached the customer" in body


def test_the_note_shows_its_working(monkeypatch, corpus, wired):
    client = _install(monkeypatch, FakeClient([_comment(1, CUSTOMER_ID)]))
    suggest.suggest_for_ticket(5, corpus)
    body = client.notes[0][1]
    assert "confidence: high" in body
    assert "policy rules: 2, 16" in body
    assert "past tickets: 1234" in body


# --- the webhook endpoint -------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from evogolf_support.api import app as app_module

    monkeypatch.setenv("AUTO_EXPORT", "false")
    monkeypatch.setenv("AUTO_MINE", "false")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-tok")
    monkeypatch.setenv("ZENDESK_WEBHOOK_TOKEN", "hook-tok")
    with TestClient(app_module.app) as c:
        yield c


def _hook(api, token, ticket_id=5):
    return api.post("/zendesk/hook", json={"ticket_id": ticket_id},
                    headers={"Authorization": f"Bearer {token}"})


def test_the_hook_needs_its_token(api, monkeypatch):
    monkeypatch.setattr(suggest, "suggest_for_ticket", lambda *a, **k: "suggested")
    assert _hook(api, "hook-tok").status_code == 202
    assert _hook(api, "admin-tok").status_code == 401
    assert _hook(api, "").status_code == 401
    assert api.post("/zendesk/hook", json={"ticket_id": 5}).status_code == 401


def test_the_hook_is_disabled_when_no_token_is_set(api, monkeypatch):
    monkeypatch.delenv("ZENDESK_WEBHOOK_TOKEN")
    monkeypatch.delenv("ADMIN_TOKEN")
    assert _hook(api, "anything").status_code == 403


def test_the_webhook_token_is_not_the_admin_token(api, monkeypatch):
    """Zendesk admins can read it, so it must not unlock export or reindex."""
    assert api.post("/reindex", headers={"Authorization": "Bearer hook-tok"}
                    ).status_code == 401


def test_the_hook_answers_before_it_drafts(api, monkeypatch):
    """Drafting outlasts Zendesk's patience, and a timeout means a retry."""
    done: list[int] = []
    monkeypatch.setattr(suggest, "suggest_for_ticket",
                        lambda tid, corpus: done.append(tid) or "suggested")
    r = _hook(api, "hook-tok", ticket_id=77)
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    assert done == [77]          # TestClient runs background tasks on exit


def test_a_failed_draft_does_not_escape_the_background_task(api, monkeypatch):
    """A 202 is already sent, so Zendesk never retries. Failure must be logged."""
    def boom(tid, corpus):
        raise RuntimeError("Anthropic is down")

    monkeypatch.setattr(suggest, "suggest_for_ticket", boom)
    assert _hook(api, "hook-tok").status_code == 202


def test_a_quoted_ticket_id_is_accepted(api, monkeypatch):
    """Zendesk's JSON body editor flags an unquoted placeholder as invalid JSON,
    so admins quote it. {{ticket.id}} then arrives as a string."""
    seen: list[int] = []
    monkeypatch.setattr(suggest, "suggest_for_ticket",
                        lambda tid, corpus: seen.append(tid) or "suggested")
    r = api.post("/zendesk/hook", json={"ticket_id": "31204"},
                 headers={"Authorization": "Bearer hook-tok"})
    assert r.status_code == 202
    assert seen == [31204]


def test_a_payload_without_a_ticket_id_is_rejected(api):
    """Zendesk's Test webhook button sends its own sample payload."""
    r = api.post("/zendesk/hook", json={"hello": "world"},
                 headers={"Authorization": "Bearer hook-tok"})
    assert r.status_code == 422
