"""Learning from the replies the team sent instead of the suggestion.

A draft is a guess; the reply a person chose to send is the answer. Where
the two differ, that pair is the most direct evidence there is of how this
business wants these handled - and it is worth more than older examples
that were never measured against an attempt.
"""

import datetime as dt

import pytest

from evogolf_support import learning, metrics
from evogolf_support.corpus.store import CorpusStore

AGENT, CUSTOMER = 1, 2
DRAFT = ("Hi Craig,\n\nYour Motocaddy left us on 16 September with DPD, tracking "
         "15488234901. I am chasing them today.\n\nMany thanks, Evo Support Team")
REWRITTEN = ("Hi Craig,\n\nI have been on to DPD this morning and they have opened a "
             "trace. They come back within two working days and I will ring you the "
             "moment I hear. If it cannot be found we will rebuild the order from "
             "stock.\n\nMany thanks, Evo Support Team")


def _iso(hours_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def store(tmp_path):
    with CorpusStore(tmp_path / "c.sqlite3") as s:
        s.upsert_users([{"id": AGENT, "name": "Brad", "email": "b@e.co", "role": "agent"},
                        {"id": CUSTOMER, "name": "C", "email": "c@e.co",
                         "role": "end-user"}])
        yield s


def _ticket(store, tid, sent, draft=DRAFT, handover=False, theme=None):
    store.upsert_ticket({"id": tid, "subject": "Where is my order", "status": "open",
                         "created_at": _iso(6), "requester_id": CUSTOMER,
                         "via": {"channel": "email"}})
    comments = [{"id": tid * 10, "author_id": CUSTOMER, "public": True,
                 "created_at": _iso(6), "body": "q",
                 "clean_body": "My trolley still has not arrived, where is it?"}]
    if sent:
        comments.append({"id": tid * 10 + 1, "author_id": AGENT, "public": True,
                         "created_at": _iso(3), "body": sent, "clean_body": sent})
    store.replace_comments(tid, comments)
    if theme:
        store.set_ticket_themes({tid: theme})
    store.record_draft(tid, tid * 10, _iso(5), draft, "high", handover)


# --- what gets learned from ----------------------------------------------

def test_a_rewritten_reply_is_kept(store):
    _ticket(store, 1, REWRITTEN)
    assert learning.collect(store) == 1
    kept = store.recent_corrections(None)
    assert len(kept) == 1
    assert "opened a trace" in kept[0]["sent"]
    assert "chasing them today" in kept[0]["suggested"]
    assert "still has not arrived" in kept[0]["question"]


def test_a_draft_sent_as_written_teaches_nothing(store):
    _ticket(store, 1, DRAFT)
    assert learning.collect(store) == 0


def test_a_handover_is_not_a_correction(store):
    """No draft was written, so the agent's reply is not a rewrite of one."""
    _ticket(store, 1, REWRITTEN, draft="", handover=True)
    assert learning.collect(store) == 0


def test_a_two_line_acknowledgement_is_not_worth_learning_from(store):
    _ticket(store, 1, "Thanks, looking into it now.")
    assert learning.collect(store) == 0


def test_a_ticket_with_no_reply_yet_is_left_for_later(store):
    _ticket(store, 1, None)
    assert learning.collect(store) == 0


def test_collecting_twice_does_not_duplicate(store):
    _ticket(store, 1, REWRITTEN)
    assert learning.collect(store) == 1
    assert learning.collect(store) == 0
    assert len(store.recent_corrections(None, limit=10)) == 1


def test_a_customer_reply_is_never_taken_as_a_correction(store):
    store.upsert_ticket({"id": 1, "subject": "s", "status": "open",
                         "created_at": _iso(6), "requester_id": CUSTOMER,
                         "via": {"channel": "email"}})
    store.replace_comments(1, [
        {"id": 10, "author_id": CUSTOMER, "public": True, "created_at": _iso(6),
         "body": "q", "clean_body": "Where is it?"},
        {"id": 11, "author_id": CUSTOMER, "public": True, "created_at": _iso(3),
         "body": REWRITTEN, "clean_body": REWRITTEN},
    ])
    store.record_draft(1, 10, _iso(5), DRAFT, "high", False)
    assert learning.collect(store) == 0


# --- how they are chosen for a prompt ------------------------------------

def test_the_tickets_own_theme_is_preferred(store):
    _ticket(store, 1, REWRITTEN, theme="delivery_tracking")
    _ticket(store, 2, REWRITTEN.replace("DPD", "Royal Mail"), theme="returns_refunds")
    learning.collect(store)
    chosen = store.recent_corrections("returns_refunds", limit=1)
    assert chosen[0]["ticket_id"] == 2


def test_other_themes_fill_the_gap_rather_than_returning_nothing(store):
    """A brand new theme has no corrections of its own yet."""
    _ticket(store, 1, REWRITTEN, theme="delivery_tracking")
    learning.collect(store)
    assert len(store.recent_corrections("custom_fitting", limit=3)) == 1


def test_nothing_learned_means_nothing_added_to_the_prompt(store):
    from evogolf_support.drafting.generate import _render_corrections
    assert _render_corrections([]) == ""


def test_the_prompt_block_shows_both_sides(store):
    from evogolf_support.drafting.generate import _render_corrections
    _ticket(store, 1, REWRITTEN)
    learning.collect(store)
    block = _render_corrections(store.recent_corrections(None))
    assert "What was suggested" in block and "What the team actually sent" in block
    assert "opened a trace" in block
    assert "learn from" in block.lower()
