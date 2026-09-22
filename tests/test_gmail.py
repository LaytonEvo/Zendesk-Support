"""Importing the online@ support history into the corpus.

Half the support operation never reached Zendesk. These conversations are
pre-sale and deeper than the ticket history, so getting them in matters -
but they come from a live customer-facing mailbox, which is why the access
is read-only and the import is re-runnable rather than additive.
"""

import base64

import pytest

from evogolf_support.corpus.store import CorpusStore
from evogolf_support.gmail import ingest, oauth

AGENT = "online@evolutiongolf.co.uk"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(mid, sender, body, subject="Custom fitting", stamp=1789000000000,
         mime="text/plain"):
    return {
        "id": mid,
        "internalDate": str(stamp),
        "payload": {
            "mimeType": mime,
            "headers": [{"name": "From", "value": sender},
                        {"name": "Subject", "value": subject}],
            "body": {"data": _b64(body)},
        },
    }


@pytest.fixture
def store(tmp_path):
    with CorpusStore(tmp_path / "c.sqlite3") as s:
        yield s


@pytest.fixture
def gmail(monkeypatch):
    """A fake Gmail. Records every request so reads can be proved read-only."""
    state = {"threads": {}, "calls": []}

    def fake_get(path, token, **params):
        state["calls"].append((path, params))
        if path == "labels":
            return {"labels": [
                {"id": "L1", "name": "Customer Email Enquiries"},
                {"id": "L2", "name": "Custom Fitting Enquiries"},
            ]}
        if path == "threads":
            label = params.get("labelIds")
            return {"threads": [{"id": t} for t, (lab, _) in state["threads"].items()
                                if lab == label]}
        if path.startswith("threads/"):
            return {"messages": state["threads"][path.split("/", 1)[1]][1]}
        raise AssertionError(path)

    monkeypatch.setattr(ingest, "_get", fake_get)
    monkeypatch.setattr(ingest.oauth, "access_token", lambda store: "tok")
    return state


def _thread(gmail, tid, messages, label="L1"):
    gmail["threads"][tid] = (label, messages)


# --- the import ----------------------------------------------------------

def test_a_conversation_becomes_a_ticket_with_both_sides(store, gmail):
    _thread(gmail, "t1", [
        _msg("m1", "Nick Dodd <nick@example.com>", "Can I book a driver fitting?"),
        _msg("m2", f"Online Evolution <{AGENT}>", "Yes - week commencing 5 October."),
    ])
    result = ingest.import_threads(store)
    assert result["threads"] == 1 and result["messages"] == 2

    ticket_id = ingest.thread_key("t1")
    rows = store._conn.execute(
        "SELECT author_id, clean_body FROM comments WHERE ticket_id=? "
        "ORDER BY created_at", (ticket_id,)).fetchall()
    assert len(rows) == 2
    assert "driver fitting" in rows[0]["clean_body"]


def test_our_replies_are_recorded_as_agent_replies(store, gmail):
    """Otherwise every thread reads as a customer talking to themselves, and
    the evaluation has no reply to compare a draft against."""
    _thread(gmail, "t1", [
        _msg("m1", "nick@example.com", "Can I book a fitting?"),
        _msg("m2", AGENT, "Yes, from 5 October."),
    ])
    ingest.import_threads(store)
    agents = store.agent_ids()
    rows = store._conn.execute("SELECT author_id FROM comments").fetchall()
    assert any(r["author_id"] in agents for r in rows)
    assert any(r["author_id"] not in agents for r in rows)


def test_re_running_updates_rather_than_duplicates(store, gmail):
    """Ids come from the Gmail thread, so a second run is not a second copy."""
    _thread(gmail, "t1", [
        _msg("m1", "nick@example.com", "Question?"),
        _msg("m2", AGENT, "Answer."),
    ])
    ingest.import_threads(store)
    ingest.import_threads(store)
    assert store._conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"] == 1
    assert store._conn.execute("SELECT COUNT(*) c FROM comments").fetchone()["c"] == 2


def test_imported_ids_cannot_collide_with_zendesk_tickets(store, gmail):
    """Zendesk ids run in the low thousands; these must stay clear of them."""
    assert ingest.thread_key("t1") >= ingest.ID_BASE
    assert ingest.person_key("a@b.c") >= ingest.ID_BASE


def test_a_thread_in_both_labels_is_imported_once(store, gmail, monkeypatch):
    messages = [_msg("m1", "nick@example.com", "Question?"),
                _msg("m2", AGENT, "Answer.")]

    def both(path, token, **params):
        if path == "labels":
            return {"labels": [{"id": "L1", "name": "Customer Email Enquiries"},
                               {"id": "L2", "name": "Custom Fitting Enquiries"}]}
        if path == "threads":
            return {"threads": [{"id": "t1"}]}          # returned under both
        return {"messages": messages}

    monkeypatch.setattr(ingest, "_get", both)
    assert ingest.import_threads(store)["threads"] == 1


# --- what must not be imported -------------------------------------------

def test_a_thread_with_no_reply_is_skipped(store, gmail):
    """A question with no answer teaches nothing."""
    _thread(gmail, "t1", [_msg("m1", "nick@example.com", "Anyone there?")])
    result = ingest.import_threads(store)
    assert result["threads"] == 0 and result["skipped_empty"] == 1


def test_a_thread_with_no_customer_is_skipped(store, gmail):
    """Internal chatter between our own addresses is not support history."""
    _thread(gmail, "t1", [
        _msg("m1", AGENT, "Forwarding this on."),
        _msg("m2", "jack@evolutiongolf.co.uk", "Got it."),
    ])
    assert ingest.import_threads(store)["threads"] == 0


def test_a_renamed_label_fails_loudly(store, gmail, monkeypatch):
    """Silently importing nothing is the failure mode to avoid here."""
    monkeypatch.setattr(ingest, "_get", lambda path, token, **kw: {"labels": []})
    with pytest.raises(RuntimeError, match="labels not found"):
        ingest.import_threads(store)


def test_the_import_only_ever_reads(store, gmail):
    """It reads a live customer-facing mailbox."""
    _thread(gmail, "t1", [
        _msg("m1", "nick@example.com", "Q?"), _msg("m2", AGENT, "A.")])
    ingest.import_threads(store)
    assert all(p in ("labels", "threads") or p.startswith("threads/")
               for p, _ in gmail["calls"])


def test_the_scope_requested_is_read_only():
    assert oauth.SCOPES == "https://www.googleapis.com/auth/gmail.readonly"


# --- message parsing -----------------------------------------------------

def test_plain_text_is_preferred_over_the_html_alternative(store):
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": _b64("<p>HTML version</p>")}},
        {"mimeType": "text/plain", "body": {"data": _b64("Plain version")}},
    ]}
    assert ingest.message_text(payload).strip() == "Plain version"


def test_html_only_mail_still_yields_text(store):
    payload = {"mimeType": "text/html",
               "body": {"data": _b64("<p>Hello <b>Nick</b></p>")}}
    text = ingest.message_text(payload)
    assert "Hello" in text and "<p>" not in text


def test_a_nested_multipart_body_is_found(store):
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Buried but found")}},
        ]},
    ]}
    assert "Buried but found" in ingest.message_text(payload)


# --- the admin token must not reach the logs -----------------------------
#
# Found live: authorising Gmail wrote the full request line into Railway's
# deploy log, admin token and all. Two endpoints have to accept the token in
# the query string because a browser cannot send a header from the address
# bar, so the redaction has to happen at the logger.

def test_a_query_token_is_redacted_from_the_access_log():
    import logging
    from evogolf_support.api.app import _RedactQueryToken

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d', (
                                   "100.64.0.1:15854", "GET",
                                   "/google/install?token=B2z3s5id3s", "1.1", 307,
                               ), None)
    _RedactQueryToken().filter(record)
    rendered = record.getMessage()
    assert "B2z3s5id3s" not in rendered
    assert "token=REDACTED" in rendered
    assert "/google/install" in rendered          # the path itself still logs


def test_redaction_survives_extra_query_parameters():
    import logging
    from evogolf_support.api.app import _RedactQueryToken

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               "%s", ("/proactive/preview?token=abc123&unfulfilled_days=0",),
                               None)
    _RedactQueryToken().filter(record)
    rendered = record.getMessage()
    assert "abc123" not in rendered
    assert "unfulfilled_days=0" in rendered      # other parameters are kept


def test_redaction_also_covers_our_own_log_lines():
    import logging
    from evogolf_support.api.app import _RedactQueryToken

    record = logging.LogRecord("x", logging.INFO, "", 0,
                               "Opened https://host/google/install?token=secret99", (), None)
    _RedactQueryToken().filter(record)
    assert "secret99" not in record.getMessage()
