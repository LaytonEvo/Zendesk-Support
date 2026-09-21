"""Mining selection and thread assembly."""

from evogolf_support.corpus.store import CorpusStore
from evogolf_support.mining.discover import Taxonomy, Theme
from evogolf_support.mining.mine import _threads
from evogolf_support.mining.reclassify import _ambiguous_rows
from evogolf_support.mining.run import MIN_AGENT_REPLIES, themes_worth_mining


def _taxonomy(*keys: str) -> Taxonomy:
    return Taxonomy(
        themes=[Theme(key=k, label=k, definition=f"about {k}") for k in keys],
        notes="",
    )


def _corpus(tmp_path) -> CorpusStore:
    store = CorpusStore(tmp_path / "c.sqlite3")
    store.upsert_users([{"id": 5, "name": "Brad", "role": "agent"},
                        {"id": 9, "name": "Cust", "role": "end-user"}])
    return store


def _add(store, ticket_id, theme, agent_replies, *, subject="Order 27641"):
    store.upsert_ticket({"id": ticket_id, "subject": subject, "status": "closed",
                         "created_at": f"2026-01-{ticket_id:02d}"})
    store.set_ticket_themes({ticket_id: theme})
    comments = [{"id": ticket_id * 100, "author_id": 9, "public": True,
                 "body": "b", "clean_body": "Where is my order?"}]
    for n in range(agent_replies):
        comments.append({"id": ticket_id * 100 + n + 1, "author_id": 5, "public": True,
                         "body": "b", "clean_body": f"Reply {n}"})
    store.replace_comments(ticket_id, comments)


def test_thin_themes_are_skipped(tmp_path):
    """Four agent replies cannot support a policy rule."""
    with _corpus(tmp_path) as store:
        _add(store, 1, "rich_theme", MIN_AGENT_REPLIES)
        _add(store, 2, "thin_theme", 2)
        chosen = themes_worth_mining(store, _taxonomy("rich_theme", "thin_theme"))

    assert [c[0] for c in chosen] == ["rich_theme"]


def test_non_support_themes_are_never_mined(tmp_path):
    """Spam and test tickets are excluded whatever their reply count."""
    with _corpus(tmp_path) as store:
        _add(store, 1, "b2b_supplier_marketing_pitches", MIN_AGENT_REPLIES + 20)
        _add(store, 2, "internal_test_noise", MIN_AGENT_REPLIES + 20)
        chosen = themes_worth_mining(
            store, _taxonomy("b2b_supplier_marketing_pitches", "internal_test_noise")
        )

    assert chosen == []


def test_threads_skip_tickets_with_no_agent_reply(tmp_path):
    """A ticket nobody answered teaches nothing about the team's voice."""
    with _corpus(tmp_path) as store:
        _add(store, 1, "returns", 1)
        _add(store, 2, "returns", 0)  # customer only
        threads = _threads(store, "returns")

    assert len(threads) == 1
    assert "ticket 1" in threads[0]
    assert "AGENT: Reply 0" in threads[0]
    assert "CUSTOMER: Where is my order?" in threads[0]


def test_reclassify_targets_only_ambiguous_themes(tmp_path):
    with _corpus(tmp_path) as store:
        _add(store, 1, "order_status_delivery", 1)
        _add(store, 2, "custom_fitting_services", 1)
        rows = _ambiguous_rows(store)

    assert [r.ticket_id for r in rows] == [1]
    # The opening message is the customer's, not the agent's.
    assert "Where is my order?" in rows[0].text
    assert "Reply 0" not in rows[0].text


def test_reclassify_skips_tickets_with_no_customer_message(tmp_path):
    """Nothing to re-read means the subject-based assignment stands."""
    with _corpus(tmp_path) as store:
        store.upsert_ticket({"id": 1, "subject": "Order 1", "status": "closed"})
        store.set_ticket_themes({1: "order_status_delivery"})
        store.replace_comments(1, [
            {"id": 11, "author_id": 5, "public": True, "body": "b", "clean_body": "Agent only"},
        ])
        assert _ambiguous_rows(store) == []


def test_reclassify_binds_parameters_in_sql_text_order(tmp_path):
    """Regression: agent-id placeholders precede theme placeholders in the SQL.

    Binding them in the wrong order matches nothing and turns re-classification
    into a silent no-op, which looks identical to "there was nothing to do".
    """
    with _corpus(tmp_path) as store:
        # Several agents, so a mis-ordered bind cannot coincidentally line up.
        store.upsert_users([
            {"id": 5, "name": "Brad", "role": "agent"},
            {"id": 6, "name": "Jack", "role": "agent"},
            {"id": 7, "name": "Alex", "role": "admin"},
        ])
        _add(store, 1, "order_status_delivery", 1)
        _add(store, 2, "returns_exchanges_refunds", 1)
        _add(store, 3, "order_amendment_cancellation", 1)
        rows = _ambiguous_rows(store)

    assert sorted(r.ticket_id for r in rows) == [1, 2, 3]
    for row in rows:
        assert "Where is my order?" in row.text
