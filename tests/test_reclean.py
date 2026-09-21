"""Re-cleaning applies rule fixes to already-exported comments."""

from evogolf_support.corpus.clean import CLEANER_VERSION, clean_body
from evogolf_support.corpus.reclean import VERSION_KEY, needs_reclean, reclean
from evogolf_support.corpus.store import CorpusStore


def _store(tmp_path) -> CorpusStore:
    store = CorpusStore(tmp_path / "corpus.sqlite3")
    store.upsert_ticket({"id": 1, "status": "solved"})
    return store


def test_reclean_is_needed_on_a_fresh_corpus(tmp_path):
    with _store(tmp_path) as store:
        assert needs_reclean(store) is True


def test_reclean_updates_stale_clean_bodies(tmp_path):
    raw = "The refund is processed.\n\nKind regards, Brad\n"
    with _store(tmp_path) as store:
        # Simulate a row cleaned by the older rules, which left the sign-off in.
        store.replace_comments(
            1, [{"id": 7, "author_id": 5, "public": True, "body": raw,
                 "clean_body": raw}]
        )
        changed = reclean(store, redact_pii=True)
        assert changed == 1

        row = store._conn.execute(
            "SELECT clean_body FROM comments WHERE id = 7"
        ).fetchone()
        assert "Kind regards" not in row["clean_body"]
        assert "refund is processed" in row["clean_body"]


def test_reclean_records_the_version_and_is_not_repeated(tmp_path):
    with _store(tmp_path) as store:
        store.replace_comments(
            1, [{"id": 7, "author_id": 5, "public": True, "body": "Hi",
                 "clean_body": ""}]
        )
        reclean(store, redact_pii=True)
        assert store.get_state(VERSION_KEY) == str(CLEANER_VERSION)
        assert needs_reclean(store) is False


def test_reclean_reproduces_what_the_export_would_have_written(tmp_path):
    """Re-cleaning must be identical to cleaning at export time."""
    raw = (
        "Hi Jayman,\n\nI've raised a collection with DPD for Thursday.\n\n"
        "Many thanks Brad\nEvolution Golf | 01234 567890\n"
    )
    with _store(tmp_path) as store:
        store.replace_comments(
            1, [{"id": 7, "author_id": 5, "public": True, "body": raw,
                 "clean_body": "stale"}]
        )
        reclean(store, redact_pii=True)
        row = store._conn.execute(
            "SELECT clean_body FROM comments WHERE id = 7"
        ).fetchone()
        assert row["clean_body"] == clean_body(raw, redact_pii=True)
