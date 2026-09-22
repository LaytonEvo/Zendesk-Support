"""Drafts reach a person in Slack as the ticket arrives.

The point of this channel is that silence means something. A ticket that
produces no Slack message is a signal, where an unvisited Zendesk sidebar
is just an unvisited sidebar - so the posting must be reliable, and must
never be the thing that breaks a draft.
"""

import pytest

from evogolf_support import slack
from evogolf_support.drafting.generate import Draft


def _draft(**kw):
    base = dict(hand_to_agent=False, handover_reason="",
                draft="Hi Craig,\n\nYour trolley is with DPD.\n\nSupport Team",
                confidence="high", rules_applied=[2, 16], tickets_referenced=[18342],
                agent_notes=["Confirm the tracking number."])
    base.update(kw)
    return Draft(**base)


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "evolutiongolf")
    posted = []

    class Response:
        def raise_for_status(self): return None

    monkeypatch.setattr(slack.httpx, "post",
                        lambda url, json, timeout: posted.append(json) or Response())
    return posted


def _text(payload) -> str:
    """Everything a reader would see, flattened."""
    out = [payload.get("text", "")]
    for block in payload.get("blocks", []):
        if block.get("type") == "section":
            out.append(block["text"]["text"])
        for el in block.get("elements", []) or []:
            out.append(el.get("text", ""))
    return "\n".join(out)


def test_a_draft_reaches_slack(sent):
    assert slack.post_draft(1375, "Where is my order?", "It hasn't arrived.", _draft())
    body = _text(sent[0])
    assert "Your trolley is with DPD" in body
    assert "It hasn't arrived." in body
    assert "confidence: high" in body
    assert "Confirm the tracking number." in body


def test_the_message_links_to_the_ticket(sent):
    slack.post_draft(1375, "Where is my order?", "x", _draft())
    assert "https://evolutiongolf.zendesk.com/agent/tickets/1375" in _text(sent[0])


def test_it_says_the_customer_has_not_seen_it(sent):
    slack.post_draft(1375, "s", "x", _draft())
    assert "nothing has reached the customer" in _text(sent[0]).lower()


def test_a_handover_posts_the_reason_and_no_draft(sent):
    slack.post_draft(1375, "Refund please", "I want a refund.", _draft(
        hand_to_agent=True, handover_reason="Rule 3: refunds need a person.",
        draft="Here is your refund, no questions asked."))
    body = _text(sent[0])
    assert "Needs an agent" in body and "Rule 3" in body
    assert "no questions asked" not in body


def test_a_long_customer_message_is_trimmed(sent):
    slack.post_draft(1375, "s", "x" * 5000, _draft())
    for block in sent[0]["blocks"]:
        if block.get("type") == "section":
            assert len(block["text"]["text"]) <= slack.BLOCK_LIMIT


def test_nothing_is_posted_without_a_webhook(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(slack.httpx, "post",
                        lambda *a, **k: pytest.fail("must not call Slack"))
    assert slack.post_draft(1, "s", "x", _draft()) is False


def test_slack_being_down_does_not_raise(monkeypatch):
    """The note is already on the ticket. Slack failing must not undo that."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(slack.httpx, "post", boom)
    assert slack.post_draft(1, "s", "x", _draft()) is False


def test_a_failed_post_never_costs_the_draft(monkeypatch, tmp_path):
    """The ticket is where the draft has to be; Slack is where it is read."""
    from evogolf_support.corpus.store import CorpusStore
    from evogolf_support.zendesk import suggest
    from test_suggest import FakeClient, _comment, CUSTOMER_ID

    path = tmp_path / "c.sqlite3"
    with CorpusStore(path):
        pass
    suggest._api_user_id = None
    monkeypatch.setattr(suggest.shopify, "context_for_ticket", lambda *a, **k: "")
    monkeypatch.setattr(suggest, "draft_reply", lambda store, **kw: _draft())
    monkeypatch.setattr(suggest.slack, "post_draft",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("slack down")))
    client = FakeClient([_comment(1, CUSTOMER_ID)])
    monkeypatch.setattr(suggest, "ZendeskClient", lambda *a, **k: client)

    assert suggest.suggest_for_ticket(5, path) == "suggested"
    assert len(client.notes) == 1        # the note landed, and is still reported
