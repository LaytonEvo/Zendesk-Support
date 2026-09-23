"""The admin dashboard: is Zendesk used, and are the drafts relied on?

The adoption number is the one that matters, and it has to be earned from
evidence rather than asked for - so it compares each draft against the reply
the agent actually sent.
"""

import datetime as dt

import pytest

from evogolf_support import metrics
from evogolf_support.api import dashboard
from evogolf_support.corpus.store import CorpusStore

DRAFT = ("Hi Craig,\n\nYour Motocaddy left us on 16 September with DPD, tracking "
         "15488234901. I am chasing them today.\n\nMany thanks, Evo Support Team")
AGENT, CUSTOMER = 1, 2


W = metrics.resolve_range("last30")


def _iso(hours_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def store(tmp_path):
    with CorpusStore(tmp_path / "c.sqlite3") as s:
        s.upsert_users([{"id": AGENT, "name": "Brad", "email": "b@e.co", "role": "agent"},
                        {"id": CUSTOMER, "name": "Craig", "email": "c@e.co",
                         "role": "end-user"}])
        yield s


def _ticket(store, tid, reply=None, draft=DRAFT, handover=False, hours=6):
    store.upsert_ticket({"id": tid, "subject": "Where is my order",
                         "status": "open", "created_at": _iso(hours),
                         "requester_id": CUSTOMER, "via": {"channel": "email"}})
    comments = [{"id": tid * 10, "author_id": CUSTOMER, "public": True,
                 "created_at": _iso(hours), "body": "q", "clean_body": "Where is it?"}]
    if reply is not None:
        comments.append({"id": tid * 10 + 1, "author_id": AGENT, "public": True,
                         "created_at": _iso(hours - 2), "body": reply,
                         "clean_body": reply})
    store.replace_comments(tid, comments)
    if draft is not None:
        store.record_draft(tid, tid * 10, _iso(hours - 1), draft, "high", handover)


# --- adoption ------------------------------------------------------------

def test_a_reply_sent_as_written_counts_as_used(store):
    _ticket(store, 1, reply=DRAFT)
    a = metrics.draft_adoption(store, W)
    assert a["used_as_is"] == 1 and a["adoption_percent"] == 100


def test_a_trivially_tweaked_reply_still_counts_as_sent_as_written(store):
    """Changing a greeting is not writing your own reply."""
    _ticket(store, 1, reply=DRAFT.replace("Craig", "Mr Whitfield"))
    a = metrics.draft_adoption(store, W)
    assert a["used_as_is"] == 1


def test_a_full_reword_is_reported_as_low_overlap_not_as_ignored(store):
    """The honest limit of this measure.

    A reply keeping the draft's facts but rewriting every sentence scores
    ~0.32; an unrelated reply that merely shares the sign-off scores ~0.29.
    Nothing separates them, so the bottom bucket claims no intent - it says
    the wording did not survive, and the headline is a floor on adoption.
    """
    edited = ("Morning Mr Whitfield,\n\nApologies for the wait on this one. Your "
              "Motocaddy went out with DPD on the 16th, tracking 15488234901, and "
              "it has stalled at their depot. I have put a priority trace on it "
              "this morning and will come back to you the moment they reply.\n\n"
              "Many thanks, Evo Support Team")
    _ticket(store, 1, reply=edited)
    a = metrics.draft_adoption(store, W)
    assert a["low_overlap"] == 1


def test_an_unrelated_reply_does_not_count(store):
    _ticket(store, 1, reply="Hi, I've spoken to the warehouse and it goes out "
                            "tomorrow. Sorry for the wait.")
    a = metrics.draft_adoption(store, W)
    assert a["low_overlap"] == 1 and a["adoption_percent"] == 0


def test_a_draft_with_no_reply_yet_is_not_counted_either_way(store):
    """Counting an unanswered ticket as ignored would flatter or damn it unfairly."""
    _ticket(store, 1, reply=None)
    a = metrics.draft_adoption(store, W)
    assert a["no_reply_yet"] == 1 and a["judged"] == 0
    assert a["adoption_percent"] is None


def test_a_handover_is_excluded_from_adoption(store):
    """No draft was written, so there is nothing that could have been adopted."""
    _ticket(store, 1, reply="Handled it myself.", draft="", handover=True)
    a = metrics.draft_adoption(store, W)
    assert a["handover"] == 1 and a["judged"] == 0


def test_a_customer_reply_is_not_mistaken_for_the_agent_using_the_draft(store):
    """The customer quoting our draft back is not adoption."""
    store.upsert_ticket({"id": 1, "subject": "s", "status": "open",
                         "created_at": _iso(6), "requester_id": CUSTOMER,
                         "via": {"channel": "email"}})
    store.replace_comments(1, [
        {"id": 10, "author_id": CUSTOMER, "public": True, "created_at": _iso(6),
         "body": "q", "clean_body": "Where is it?"},
        {"id": 11, "author_id": CUSTOMER, "public": True, "created_at": _iso(3),
         "body": DRAFT, "clean_body": DRAFT},
    ])
    store.record_draft(1, 10, _iso(5), DRAFT, "high", False)
    a = metrics.draft_adoption(store, W)
    assert a["no_reply_yet"] == 1 and a["judged"] == 0


def test_mixed_use_reports_a_sensible_percentage(store):
    _ticket(store, 1, reply=DRAFT, hours=10)
    _ticket(store, 2, reply=DRAFT.replace("Craig", "Dave"), hours=9)
    _ticket(store, 3, reply="Completely different answer about a refund.", hours=8)
    _ticket(store, 4, reply="Nothing like the draft at all, unrelated entirely.", hours=7)
    a = metrics.draft_adoption(store, W)
    assert a["judged"] == 4
    assert a["adoption_percent"] == 50


# --- usage ---------------------------------------------------------------

def test_usage_counts_answered_and_unanswered(store):
    _ticket(store, 1, reply="Answered")
    _ticket(store, 2, reply=None)
    u = metrics.usage(store, W)
    assert u["tickets"] == 2 and u["tickets_answered"] == 1 and u["unanswered"] == 1


def test_old_tickets_fall_outside_the_window(store):
    _ticket(store, 1, reply="Answered", hours=24 * 60)
    assert metrics.usage(store, W)["tickets"] == 0


# --- the page ------------------------------------------------------------

def test_the_page_renders_with_no_data_at_all(store):
    """The first week has no data, and a wall of zeroes dressed as results is
    worse than saying plainly that it is still counting."""
    html = dashboard.render(metrics.report(store, W))
    assert "No tickets yet" in html
    assert "Too early to judge" in html
    assert "nothing to measure" in html


def test_the_page_leads_with_the_adoption_number(store):
    _ticket(store, 1, reply=DRAFT)
    html = dashboard.render(metrics.report(store, W))
    assert '<div class="big">100%</div>' in html
    assert "relied on" in html


def test_low_adoption_is_called_out_plainly(store):
    for i in range(1, 5):
        _ticket(store, i, reply="Nothing like it, written from scratch entirely.",
                hours=10 - i)
    html = dashboard.render(metrics.report(store, W))
    assert "Little sign the drafts are being used" in html
    assert "rewritten so heavily" in html          # the caveat travels with it


def test_the_page_carries_no_customer_details(store):
    _ticket(store, 1, reply=DRAFT)
    html = dashboard.render(metrics.report(store, W))
    assert "Craig" not in html and "c@e.co" not in html


def test_both_themes_are_defined(store):
    html = dashboard.render(metrics.report(store, W))
    assert "prefers-color-scheme:dark" in html
    assert '[data-theme="dark"]' in html


# --- the endpoint --------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from evogolf_support.api import app as app_module

    monkeypatch.setenv("AUTO_EXPORT", "false")
    monkeypatch.setenv("AUTO_MINE", "false")
    db = tmp_path / "c.sqlite3"
    with CorpusStore(db):
        pass                                   # the corpus exists in production
    monkeypatch.setenv("CORPUS_DB", str(db))
    monkeypatch.setenv("ADMIN_TOKEN", "admin-tok")
    monkeypatch.setenv("DASHBOARD_TOKEN", "dash-tok")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "let-me-in")
    with TestClient(app_module.app) as c:
        yield c


def test_signing_in_with_the_password_opens_the_dashboard(api):
    r = api.post("/dashboard/login", data={"password": "let-me-in"},
                 follow_redirects=True)
    assert r.status_code == 200 and "Support dashboard" in r.text
    assert "Zendesk activity" in r.text


def test_the_login_page_is_shown_until_you_sign_in(api):
    r = api.get("/dashboard")
    assert r.status_code == 401
    assert "Sign in" in r.text
    assert "Zendesk activity" not in r.text        # no figures leak to a stranger


def test_a_wrong_password_is_refused(api):
    r = api.post("/dashboard/login", data={"password": "nope"})
    assert r.status_code == 401 and "not right" in r.text
    assert api.cookies.get("evo_dash") is None


def test_a_forged_session_cookie_is_refused(api):
    api.cookies.set("evo_dash", "99999999999.deadbeef")
    assert api.get("/dashboard").status_code == 401


def test_an_expired_session_is_refused(api, monkeypatch):
    from evogolf_support.api import app as app_module
    stale = app_module._sign(int(__import__("time").time()) - 60)
    api.cookies.set("evo_dash", stale)
    assert api.get("/dashboard").status_code == 401


def test_changing_the_password_invalidates_existing_sessions(api, monkeypatch):
    """The only lever available when someone leaves."""
    api.post("/dashboard/login", data={"password": "let-me-in"})
    assert api.get("/dashboard").status_code == 200
    monkeypatch.setenv("DASHBOARD_PASSWORD", "something-else")
    assert api.get("/dashboard").status_code == 401


def test_signing_out_clears_the_session(api):
    api.post("/dashboard/login", data={"password": "let-me-in"})
    api.get("/dashboard/logout")
    assert api.get("/dashboard").status_code == 401


def test_the_dashboard_password_does_not_unlock_anything_else(api):
    """Luke gets this password. It must not carry admin."""
    assert api.post("/reindex", headers={"Authorization": "Bearer dash-tok"}
                    ).status_code == 401
    assert api.post("/zendesk/hook", json={"ticket_id": 1},
                    headers={"Authorization": "Bearer dash-tok"}).status_code == 401


def test_the_date_filters_change_the_window(api):
    api.post("/dashboard/login", data={"password": "let-me-in"})
    assert "Week to date" in api.get("/dashboard", params={"range": "week"}).text
    assert "Yesterday" in api.get("/dashboard", params={"range": "yesterday"}).text
    r = api.get("/dashboard", params={"range": "custom", "start": "2026-09-01",
                                      "end": "2026-09-10"})
    assert "1 Sep to 10 Sep 2026" in r.text


def test_a_nonsense_range_falls_back_rather_than_erroring(api):
    """This URL gets edited and shared."""
    api.post("/dashboard/login", data={"password": "let-me-in"})
    r = api.get("/dashboard", params={"range": "banana"})
    assert r.status_code == 200 and "Last 30 days" in r.text
