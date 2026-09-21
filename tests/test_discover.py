"""Theme discovery: what leaves the corpus, and what comes back."""

from evogolf_support.corpus.store import CorpusStore
from evogolf_support.mining.discover import _ticket_lines


def test_subjects_are_redacted_before_leaving_the_corpus(tmp_path):
    """Subjects carry customer details; they must not be sent verbatim."""
    with CorpusStore(tmp_path / "c.sqlite3") as store:
        store.upsert_ticket({
            "id": 1,
            "subject": "Order 27641 - call me on 07700 900123 or jay@example.com",
            "status": "closed",
            "tags": ["return"],
        })
        rows = store._conn.execute(
            "SELECT id, subject, tags FROM tickets"
        ).fetchall()

    rendered = _ticket_lines(rows)
    assert "[PHONE]" in rendered
    assert "[EMAIL]" in rendered
    assert "07700" not in rendered
    assert "example.com" not in rendered
    # The order reference is context worth keeping.
    assert "27641" in rendered
    assert "[tags: return]" in rendered


def test_missing_subject_does_not_break_the_listing(tmp_path):
    with CorpusStore(tmp_path / "c.sqlite3") as store:
        store.upsert_ticket({"id": 1, "subject": None, "status": "closed"})
        rows = store._conn.execute("SELECT id, subject, tags FROM tickets").fetchall()
    assert "(no subject)" in _ticket_lines(rows)


def test_theme_counts_and_evidence(tmp_path):
    with CorpusStore(tmp_path / "c.sqlite3") as store:
        store.upsert_users([{"id": 5, "name": "Brad", "role": "agent"}])
        for i in (1, 2, 3):
            store.upsert_ticket({"id": i, "subject": "x", "status": "closed"})
        store.set_ticket_themes({1: "trolleys", 2: "trolleys", 3: "fitting"})
        store.replace_comments(1, [
            {"id": 11, "author_id": 5, "public": True, "body": "b", "clean_body": "Reply"},
        ])

        assert store.theme_counts() == {"trolleys": 2, "fitting": 1}
        assert store.theme_agent_replies() == {"trolleys": 1}
