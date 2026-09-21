"""Theme ranking from tags and subjects - no model, no message content."""

from evogolf_support.corpus.store import CorpusStore
from evogolf_support.corpus.themes import report


def _corpus(tmp_path) -> CorpusStore:
    store = CorpusStore(tmp_path / "corpus.sqlite3")
    store.upsert_users([{"id": 5, "name": "Brad", "role": "agent"},
                        {"id": 9, "name": "Customer", "role": "end-user"}])
    return store


def test_themes_rank_by_ticket_volume(tmp_path):
    with _corpus(tmp_path) as store:
        for i in range(5):
            store.upsert_ticket({"id": i + 1, "subject": "Refund for my order",
                                 "status": "closed", "tags": ["return"]})
        store.upsert_ticket({"id": 50, "subject": "DPD tracking number",
                             "status": "closed", "tags": ["delivery"]})

        out = report(store)
        ranked = list(out["themes_by_volume"])
        assert ranked[0] == "returns_refunds"
        assert out["themes_by_volume"]["returns_refunds"]["tickets"] == 5
        assert out["themes_by_volume"]["delivery_tracking"]["tickets"] == 1


def test_deleted_tickets_are_excluded(tmp_path):
    with _corpus(tmp_path) as store:
        store.upsert_ticket({"id": 1, "subject": "Refund", "status": "deleted",
                             "tags": ["return"]})
        assert report(store)["tickets_considered"] == 0


def test_agent_reply_evidence_is_counted_per_theme(tmp_path):
    """A theme with few agent replies cannot support a confident rule."""
    with _corpus(tmp_path) as store:
        store.upsert_ticket({"id": 1, "subject": "Refund please", "status": "closed",
                             "tags": ["return"]})
        store.replace_comments(1, [
            {"id": 1, "author_id": 9, "public": True, "body": "x", "clean_body": "Can I refund?"},
            {"id": 2, "author_id": 5, "public": True, "body": "y", "clean_body": "Yes, posting a label."},
            {"id": 3, "author_id": 5, "public": True, "body": "z", "clean_body": "   "},  # empty
        ])
        evidence = report(store)["themes_by_volume"]["returns_refunds"]
        assert evidence["tickets"] == 1
        assert evidence["agent_replies"] == 1  # customer and blank replies excluded


def test_report_exposes_no_bodies(tmp_path):
    with _corpus(tmp_path) as store:
        store.upsert_ticket({"id": 1, "subject": "Refund request", "status": "closed",
                             "tags": ["return"]})
        store.replace_comments(1, [
            {"id": 1, "author_id": 5, "public": True, "body": "b",
             "clean_body": "Collected from 12 Example Street"},
        ])
        rendered = repr(report(store)).lower()

    assert "example street" not in rendered


def test_one_off_subject_words_are_not_reported(tmp_path):
    """Subject lines carry customer names; a name in one ticket must not surface."""
    with _corpus(tmp_path) as store:
        store.upsert_ticket({"id": 1, "subject": "Refund for Jayman Patel",
                             "status": "closed", "tags": ["return"]})
        # A word that recurs across tickets is a product or a problem, not a name.
        for i in range(2, 6):
            store.upsert_ticket({"id": i, "subject": "Motocaddy trolley refund",
                                 "status": "closed", "tags": ["return"]})

        words = report(store)["top_subject_words"]

    rendered = repr(words).lower()
    assert "patel" not in rendered
    assert "jayman" not in rendered
    assert words.get("motocaddy") == 4
