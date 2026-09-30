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


# --- keeping themes up to date -------------------------------------------
#
# Themes were assigned once, during discovery. Everything that arrived
# afterwards - and all 377 conversations imported from the online@ mailbox -
# had none, so retrieval fell back to plain text search for them.

def _theme_corpus(tmp_path, tickets, themed=()):
    from evogolf_support.corpus.store import CorpusStore
    path = tmp_path / "c.sqlite3"
    with CorpusStore(path) as store:
        for t in tickets:
            store.upsert_ticket(t)
        if themed:
            store.set_ticket_themes(dict(themed))
    return path


def test_only_unthemed_tickets_are_picked_up(tmp_path):
    from evogolf_support.corpus.store import CorpusStore
    from evogolf_support.mining.discover import unthemed_tickets

    path = _theme_corpus(tmp_path, [
        {"id": 1, "subject": "Already sorted", "status": "open"},
        {"id": 2, "subject": "Needs a theme", "status": "open"},
        {"id": 3, "subject": "Deleted one", "status": "deleted"},
    ], themed=[(1, "delivery_tracking")])

    with CorpusStore(path) as store:
        rows = unthemed_tickets(store)
    assert [r["id"] for r in rows] == [2]        # not 1, not the deleted 3


def test_a_backlog_is_capped_per_pass(tmp_path):
    """A boot should not turn into a long unattended job; the rest waits."""
    from evogolf_support.corpus.store import CorpusStore
    from evogolf_support.mining.discover import unthemed_tickets

    path = _theme_corpus(tmp_path, [{"id": i, "subject": f"t{i}", "status": "open"}
                              for i in range(1, 60)])
    with CorpusStore(path) as store:
        assert len(unthemed_tickets(store, limit=25)) == 25


def test_classifying_nothing_costs_nothing(tmp_path, monkeypatch):
    """The common case once it has caught up: no API call at all."""
    from evogolf_support.mining import discover

    monkeypatch.setattr(discover, "client",
                        lambda: pytest.fail("must not call the API"))
    assert discover.classify_rows([], object()) == {}


# --- a theme the business asked for --------------------------------------
#
# Discovery ran on 556 Zendesk tickets, when the custom fitting work lived
# entirely in a Gmail inbox. No amount of re-running it on that data would
# have produced a fitting theme, so it is stated rather than inferred.

def test_a_required_theme_is_added_to_the_stored_taxonomy(tmp_path, monkeypatch):
    from evogolf_support.corpus.store import CorpusStore
    from evogolf_support.api import app as app_module
    from evogolf_support.mining.discover import Taxonomy, Theme

    path = tmp_path / "c.sqlite3"
    existing = Taxonomy(themes=[Theme(key="product_sizing", label="Sizing",
                                      definition="Sizes.")], notes="")
    with CorpusStore(path) as store:
        store.set_state(app_module.TAXONOMY_KEY, existing.model_dump_json())
    monkeypatch.setattr(app_module, "corpus_path", lambda: path)

    app_module.add_required_themes()
    with CorpusStore(path) as store:
        after = Taxonomy.model_validate_json(store.get_state(app_module.TAXONOMY_KEY))
    assert "custom_fitting" in {t.key for t in after.themes}
    assert "product_sizing" in {t.key for t in after.themes}   # nothing lost


def test_adding_it_twice_changes_nothing(tmp_path, monkeypatch):
    from evogolf_support.corpus.store import CorpusStore
    from evogolf_support.api import app as app_module
    from evogolf_support.mining.discover import Taxonomy, Theme

    path = tmp_path / "c.sqlite3"
    with CorpusStore(path) as store:
        store.set_state(app_module.TAXONOMY_KEY,
                        Taxonomy(themes=[Theme(key="a", label="A", definition="x")],
                                 notes="").model_dump_json())
    monkeypatch.setattr(app_module, "corpus_path", lambda: path)
    app_module.add_required_themes()
    app_module.add_required_themes()
    with CorpusStore(path) as store:
        after = Taxonomy.model_validate_json(store.get_state(app_module.TAXONOMY_KEY))
    assert [t.key for t in after.themes].count("custom_fitting") == 1


def test_tickets_that_might_belong_are_reopened_for_classification(tmp_path):
    """A ticket keeps whatever theme it was given, so a theme added later
    never wins anything already assigned unless those are cleared."""
    from evogolf_support.corpus.store import CorpusStore

    path = tmp_path / "c.sqlite3"
    with CorpusStore(path) as store:
        store.upsert_ticket({"id": 1, "subject": "Driver fitting", "status": "open",
                             "tags": ["custom_fitting_enquiries"]})
        store.upsert_ticket({"id": 2, "subject": "What size grip", "status": "open",
                             "tags": []})
        store.upsert_ticket({"id": 3, "subject": "Refund", "status": "open", "tags": []})
        store.set_ticket_themes({1: "returns_refunds", 2: "product_sizing",
                                 3: "returns_refunds"})
        cleared = store.clear_themes_for_reconsideration(
            {"product_sizing"}, {"custom_fitting_enquiries"})

    assert cleared == 2                      # the fitting-tagged one and the sizing one
    with CorpusStore(path) as store:
        left = store._conn.execute(
            "SELECT ticket_id FROM ticket_themes").fetchall()
    assert [r["ticket_id"] for r in left] == [3]   # the unrelated one is untouched
