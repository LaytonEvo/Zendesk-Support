"""Retrieval and drafting: the parts that must not depend on a live model."""

import json

import pytest
from fastapi.testclient import TestClient

from evogolf_support.api import app as app_module
from evogolf_support.corpus.store import CorpusStore
from evogolf_support.drafting.retrieve import _query_terms, rebuild_index, similar
from evogolf_support.policy import as_prompt_text, load


def _corpus(tmp_path, n=3):
    store = CorpusStore(tmp_path / "c.sqlite3")
    store.upsert_users([{"id": 5, "name": "Brad", "role": "agent"},
                        {"id": 9, "name": "Cust", "role": "end-user"}])
    data = [
        (1, "Motocaddy battery fault", "battery will not charge on my trolley", "faulty_warranty"),
        (2, "Shoe return", "these shoes do not fit, sending back", "returns_exchanges_refunds"),
        (3, "Where is my order", "ordered a bag last week no tracking yet", "order_status_delivery"),
    ][:n]
    for tid, subject, body, theme in data:
        store.upsert_ticket({"id": tid, "subject": subject, "status": "closed",
                             "created_at": f"2026-0{tid}-01T09:00:00Z"})
        store.set_ticket_themes({tid: theme})
        store.replace_comments(tid, [
            {"id": tid*10, "author_id": 9, "public": True, "body": "b", "clean_body": body},
            {"id": tid*10+1, "author_id": 5, "public": True, "body": "b",
             "clean_body": "Thanks for getting in touch, we can help with that."},
        ])
    return store


# --- policy ---------------------------------------------------------------

def test_policy_has_all_sixteen_settled_rules():
    data = load()
    assert [r["id"] for r in data["rules"]] == list(range(1, 17))


def test_policy_prompt_states_it_overrides_examples():
    text = as_prompt_text().lower()
    assert "override" in text
    # The one rule that is not yet complete must say so in the prompt.
    assert "incomplete" in text
    assert "36.99" in text and "3.99" in text  # new price and the dead one


# --- retrieval ------------------------------------------------------------

def test_query_terms_strip_fts_syntax_from_customer_text():
    """A customer's punctuation must not reach the FTS parser."""
    out = _query_terms('trolley "battery" -charge NEAR/2 (fault) OR x*')
    assert '"' not in out and "*" not in out and "(" not in out and "/" not in out
    assert "trolley" in out and "battery" in out


def test_query_terms_drop_noise_and_short_words(tmp_path):
    out = _query_terms("Hi, thank you for the order with your team")
    assert out == ""  # every word is noise or too short


def test_retrieval_finds_the_relevant_ticket(tmp_path):
    with _corpus(tmp_path) as store:
        assert rebuild_index(store) == 3
        hits = similar(store, "my trolley battery will not charge")
        assert hits[0]["id"] == 1
        assert [m["who"] for m in hits[0]["messages"]] == ["CUSTOMER", "AGENT"]


def test_retrieval_can_be_confined_to_a_theme(tmp_path):
    with _corpus(tmp_path) as store:
        rebuild_index(store)
        hits = similar(store, "battery charge trolley shoes bag",
                       theme="returns_exchanges_refunds")
        assert [h["id"] for h in hits] == [2]


def test_index_excludes_non_support_tickets(tmp_path):
    with _corpus(tmp_path) as store:
        store.upsert_ticket({"id": 99, "subject": "SEO partnership opportunity",
                             "status": "closed"})
        store.set_ticket_themes({99: "b2b_supplier_marketing_pitches"})
        store.replace_comments(99, [
            {"id": 990, "author_id": 9, "public": True, "body": "b",
             "clean_body": "we offer backlink services"},
            {"id": 991, "author_id": 5, "public": True, "body": "b",
             "clean_body": "no thanks"},
        ])
        rebuild_index(store)
        assert similar(store, "backlink services partnership") == []


def test_index_excludes_tickets_with_no_agent_reply(tmp_path):
    with _corpus(tmp_path, n=1) as store:
        store.upsert_ticket({"id": 50, "subject": "Unanswered", "status": "closed"})
        store.replace_comments(50, [
            {"id": 500, "author_id": 9, "public": True, "body": "b",
             "clean_body": "unanswered question about putters"},
        ])
        rebuild_index(store)
        assert similar(store, "putters unanswered question") == []


def test_search_on_an_unbuilt_index_returns_nothing(tmp_path):
    """A missing index must degrade to no examples, not a 500."""
    with _corpus(tmp_path) as store:
        assert similar(store, "trolley battery") == []


# --- endpoint -------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_EXPORT", "false")
    monkeypatch.setenv("AUTO_MINE", "false")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("ADMIN_TOKEN", "tok")
    _corpus(tmp_path).close()
    with TestClient(app_module.app) as c:
        yield c


AUTH = {"Authorization": "Bearer tok"}


def test_draft_requires_admin(client):
    assert client.post("/draft", json={"body": "hi"}).status_code == 401


def test_draft_rejects_an_empty_body(client):
    r = client.post("/draft", json={"body": "   "}, headers=AUTH)
    assert r.status_code == 400


def test_draft_passes_the_request_through(client, monkeypatch):
    from evogolf_support.drafting.generate import Draft

    seen = {}

    def fake(store, **kw):
        seen.update(kw)
        return Draft(hand_to_agent=False, handover_reason="", draft="Hi Craig,",
                     confidence="high", rules_applied=[9], tickets_referenced=[3],
                     agent_notes=["Confirm the tracking number"])

    monkeypatch.setattr(app_module, "draft_reply", fake)
    r = client.post("/draft", headers=AUTH, json={
        "subject": "Order 27641", "body": "where is my order?",
        "order_context": "Fulfilled 2 Sep, DPD 123"})

    assert r.status_code == 200
    out = r.json()
    assert out["draft"] == "Hi Craig,"
    assert out["agent_notes"] == ["Confirm the tracking number"]
    assert seen["subject"] == "Order 27641"
    assert seen["order_context"] == "Fulfilled 2 Sep, DPD 123"


def test_reindex_endpoint(client):
    r = client.post("/reindex", headers=AUTH)
    assert r.status_code == 200 and r.json()["indexed"] == 3


def test_theme_filtered_search_binds_parameters_correctly(tmp_path):
    """Regression: the JOIN placeholder precedes MATCH in the SQL text.

    Positional binding pairs them by text position, so the query text would
    land in theme_key and the theme in MATCH - matching nothing, and looking
    exactly like "no similar tickets exist".
    """
    with _corpus(tmp_path) as store:
        rebuild_index(store)
        for theme, expected in [
            ("faulty_warranty", [1]),
            ("returns_exchanges_refunds", [2]),
            ("order_status_delivery", [3]),
        ]:
            hits = similar(store, "battery charge trolley shoes bag tracking",
                           theme=theme)
            assert [h["id"] for h in hits] == expected, theme


def test_a_ticket_is_never_retrieved_as_its_own_example(tmp_path):
    """Evaluation leakage: retrieving the answer you are predicting.

    Without the exclusion the top hit for a ticket is the ticket itself, and
    the drafts look far better than they are.
    """
    with _corpus(tmp_path) as store:
        rebuild_index(store)
        text = "battery will not charge on my trolley"
        assert similar(store, text)[0]["id"] == 1
        assert 1 not in [h["id"] for h in similar(store, text, exclude_ticket_id=1)]


def test_evaluation_samples_only_answered_support_tickets(tmp_path):
    from evogolf_support.drafting.evaluate import sample_tickets

    with _corpus(tmp_path) as store:
        # Spam, and a ticket nobody answered: neither is a fair test case.
        store.upsert_ticket({"id": 90, "subject": "SEO offer", "status": "closed",
                             "created_at": "2026-09-01T00:00:00Z"})
        store.set_ticket_themes({90: "b2b_supplier_marketing_pitches"})
        store.replace_comments(90, [
            {"id": 900, "author_id": 9, "public": True, "body": "b", "clean_body": "backlinks?"},
            {"id": 901, "author_id": 5, "public": True, "body": "b", "clean_body": "no thanks"}])
        store.upsert_ticket({"id": 91, "subject": "Ignored", "status": "closed",
                             "created_at": "2026-09-01T00:00:00Z"})
        store.replace_comments(91, [
            {"id": 910, "author_id": 9, "public": True, "body": "b", "clean_body": "hello?"}])

        ids = sample_tickets(store, limit=20)

    assert 90 not in ids and 91 not in ids
    assert sorted(ids) == [1, 2, 3]


def test_evaluation_hides_the_team_reply_from_the_draft(tmp_path, monkeypatch):
    """The draft must see only the customer's opening message."""
    from evogolf_support.drafting import evaluate as ev

    seen = {}

    def fake_draft(store, **kw):
        seen.update(kw)
        from evogolf_support.drafting.generate import Draft
        return Draft(hand_to_agent=False, handover_reason="", draft="d",
                     confidence="high", rules_applied=[], tickets_referenced=[],
                     agent_notes=[])

    monkeypatch.setattr(ev, "draft_reply", fake_draft)
    with _corpus(tmp_path) as store:
        result = ev.evaluate_ticket(store, 1)

    assert seen["body"] == "battery will not charge on my trolley"
    assert seen["exclude_ticket_id"] == 1
    assert "we can help" in result["team_actually_replied"].lower()
    # The actual reply is recorded for comparison, never fed to the model.
    assert result["team_actually_replied"] not in str(seen)


def test_policy_data_file_is_declared_as_package_data():
    """Regression: rules.json is data, so setuptools omits it unless told.

    An editable install reads it straight from src and passes; the deployed
    wheel did not contain it, and every draft failed with a missing file.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    package_data = config["tool"]["setuptools"]["package-data"]
    assert "*.json" in package_data["evogolf_support.policy"]


def test_policy_loads_through_the_package_not_the_working_directory():
    """Loading must not depend on where the process was started from."""
    import os
    from evogolf_support import policy

    cwd = os.getcwd()
    try:
        os.chdir("/")
        policy.load.cache_clear()
        assert len(policy.load()["rules"]) == 16
    finally:
        os.chdir(cwd)
        policy.load.cache_clear()
