"""Service tests, focused on the auth behaviour of the admin endpoints."""

import os

import pytest
from fastapi.testclient import TestClient

from evogolf_support.api import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Never let the test suite kick off a real Zendesk export.
    monkeypatch.setenv("AUTO_EXPORT", "false")
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with TestClient(app_module.app) as c:
        yield c


def test_health_is_open(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_stats_disabled_when_admin_token_unset(client):
    """An unset secret must mean closed, never open."""
    response = client.get("/stats")
    assert response.status_code == 403
    assert "ADMIN_TOKEN" in response.json()["detail"]


def test_export_disabled_when_admin_token_unset(client):
    assert client.post("/export").status_code == 403


def test_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    assert client.get("/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/stats").status_code == 401


def test_stats_with_valid_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    response = client.get("/stats", headers={"Authorization": "Bearer correct-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["corpus"]["tickets"] == 0
    assert body["export_running"] is False


def test_export_triggers_a_background_run(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    started: list[dict] = []
    monkeypatch.setattr(app_module, "_run_export", lambda **kw: started.append(kw))

    response = client.post(
        "/export?full=true", headers={"Authorization": "Bearer correct-token"}
    )
    assert response.status_code == 202
    assert response.json() == {"status": "started", "mode": "full"}

    # The work happens on a daemon thread; give it a moment to land.
    for _ in range(50):
        if started:
            break
        import time
        time.sleep(0.01)
    assert started == [{"full": True}]


def _wait_for(collection, tries=100):
    import time

    for _ in range(tries):
        if collection:
            return
        time.sleep(0.01)


def test_boot_export_walks_everything_when_there_is_no_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "fresh.sqlite3"))
    monkeypatch.setenv("AUTO_EXPORT", "true")
    started: list[dict] = []
    monkeypatch.setattr(app_module, "_run_export", lambda **kw: started.append(kw))

    app_module.kick_off_first_export()
    _wait_for(started)
    assert started == [{"full": True}]


def test_interrupted_export_resumes_on_the_next_boot(tmp_path, monkeypatch):
    """A part-finished export must not be stranded by its own partial rows."""
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "partial.sqlite3"))
    monkeypatch.setenv("AUTO_EXPORT", "true")
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore
    from evogolf_support.zendesk.export import CURSOR_KEY

    # Simulate a run that wrote some tickets and saved a cursor, then died.
    with CorpusStore(corpus_path()) as store:
        store.upsert_ticket({"id": 1, "subject": "x", "status": "solved"})
        store.set_state(CURSOR_KEY, "cursor-halfway")

    started: list[dict] = []
    monkeypatch.setattr(app_module, "_run_export", lambda **kw: started.append(kw))
    app_module.kick_off_first_export()
    _wait_for(started)

    # Resumes (full=False) rather than skipping or re-walking from scratch.
    assert started == [{"full": False}]


def test_auto_export_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "fresh.sqlite3"))
    monkeypatch.setenv("AUTO_EXPORT", "false")
    started: list[dict] = []
    monkeypatch.setattr(app_module, "_run_export", lambda **kw: started.append(kw))

    app_module.kick_off_first_export()
    assert started == []


def test_configure_logging_lets_info_through(caplog):
    """Regression: unconfigured logging silently swallowed all export progress."""
    import logging

    app_module.configure_logging()
    assert logging.getLogger().level <= logging.INFO
    assert logging.getLogger().handlers


def test_missing_credentials_are_reported_without_a_traceback(monkeypatch, caplog):
    """Before the token is set, the log should say what to do, not dump a stack."""
    import logging

    from evogolf_support.config import ConfigError

    def boom(**_kwargs):
        raise ConfigError("ZENDESK_EMAIL is not set. On Railway, add it in ...")

    monkeypatch.setattr("evogolf_support.zendesk.export.run_export", boom)
    with caplog.at_level(logging.ERROR):
        app_module._run_export(full=True)

    assert app_module._export_state["last_error"].startswith("ZENDESK_EMAIL is not set")
    assert app_module._export_state["running"] is False
    # A ConfigError must not be logged with exception info.
    assert all(record.exc_info is None for record in caplog.records)


def test_httpx_request_logging_is_quieted():
    """One line per request would bury the export's own progress lines."""
    import logging

    app_module.configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_quality_endpoint_requires_admin(client):
    assert client.get("/quality").status_code == 403


def test_quality_report_contains_no_message_content(tmp_path, monkeypatch):
    """The report must be safe to log and paste - counts only."""
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.quality import report
    from evogolf_support.corpus.store import CorpusStore

    secret = "Jayman's trolley was collected from 12 Example Street"
    with CorpusStore(corpus_path()) as store:
        store.upsert_ticket({"id": 1, "status": "solved", "created_at": "2026-01-01"})
        store.replace_comments(
            1, [{"id": 1, "author_id": 5, "public": True, "body": secret,
                 "clean_body": secret}]
        )
        rendered = repr(report(store))

    assert "Jayman" not in rendered
    assert "Example Street" not in rendered


def test_discovery_is_skipped_when_a_taxonomy_is_already_stored(tmp_path, monkeypatch):
    """Discovery costs real API calls - it must run once, not on every deploy."""
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setenv("AUTO_MINE", "true")
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore

    with CorpusStore(corpus_path()) as store:
        store.upsert_ticket({"id": 1, "status": "closed"})
        store.set_state(app_module.TAXONOMY_KEY, '{"themes": [], "notes": ""}')

    started: list[int] = []
    monkeypatch.setattr(app_module, "_run_discovery", lambda: started.append(1))
    app_module.kick_off_discovery()
    assert started == []


def test_discovery_is_skipped_on_an_empty_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setenv("AUTO_MINE", "true")
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore

    with CorpusStore(corpus_path()) as store:
        store.stats()

    started: list[int] = []
    monkeypatch.setattr(app_module, "_run_discovery", lambda: started.append(1))
    app_module.kick_off_discovery()
    assert started == []


def test_auto_mine_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setenv("AUTO_MINE", "false")
    started: list[int] = []
    monkeypatch.setattr(app_module, "_run_discovery", lambda: started.append(1))
    app_module.kick_off_discovery()
    assert started == []


def test_discovery_runs_when_tickets_exist_and_no_taxonomy(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setenv("AUTO_MINE", "true")
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore

    with CorpusStore(corpus_path()) as store:
        store.upsert_ticket({"id": 1, "status": "closed"})

    started: list[int] = []
    monkeypatch.setattr(app_module, "_run_discovery", lambda: started.append(1))
    app_module.kick_off_discovery()
    for _ in range(100):
        if started:
            break
        import time
        time.sleep(0.01)
    assert started == [1]
