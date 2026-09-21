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


def test_boot_export_is_skipped_when_corpus_has_tickets(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "corpus.sqlite3"))
    monkeypatch.setenv("AUTO_EXPORT", "true")
    from evogolf_support.config import corpus_path
    from evogolf_support.corpus.store import CorpusStore

    with CorpusStore(corpus_path()) as store:
        store.upsert_ticket({"id": 1, "subject": "x", "status": "solved"})

    calls: list[dict] = []
    monkeypatch.setattr(app_module, "_run_export", lambda **kw: calls.append(kw))
    app_module.kick_off_first_export()
    assert calls == []


def test_boot_export_runs_when_corpus_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORPUS_DB", str(tmp_path / "fresh.sqlite3"))
    monkeypatch.setenv("AUTO_EXPORT", "true")
    started: list[dict] = []
    monkeypatch.setattr(app_module, "_run_export", lambda **kw: started.append(kw))

    app_module.kick_off_first_export()
    for _ in range(50):
        if started:
            break
        import time
        time.sleep(0.01)
    assert started == [{"full": True}]


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
