"""HTTP service for the drafting assistant.

Runs on Railway. Two jobs today:

1. Keep the corpus current. On boot, if the corpus is empty, it exports the
   full Zendesk history in a background thread; a scheduled POST /export
   afterwards pulls only what changed.
2. Report what is in the corpus, so progress can be checked without a
   terminal.

The draft-generation endpoint for the Zendesk sidebar app lands here in
phase 3.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import ConfigError, corpus_path
from ..corpus.store import CorpusStore

log = logging.getLogger(__name__)

# Guards against a second export starting while one is already running.
_export_lock = threading.Lock()
_export_state: dict[str, Any] = {"running": False, "last_result": None, "last_error": None}

_bearer = HTTPBearer(auto_error=False)


def _admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "").strip()


def require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Gate the mutating endpoints behind ADMIN_TOKEN.

    If ADMIN_TOKEN is unset the endpoint is disabled rather than public - an
    unset secret must never mean "open to the internet".
    """
    token = _admin_token()
    if not token:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "ADMIN_TOKEN is not set, so this endpoint is disabled.",
        )
    if creds is None or creds.credentials != token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin token.")


def _run_export(**kwargs: Any) -> None:
    """Run an export, recording the outcome for /stats to report."""
    from ..zendesk.export import run_export

    if not _export_lock.acquire(blocking=False):
        log.info("Export already running; skipping this trigger")
        return
    _export_state.update(running=True, last_error=None)
    try:
        result = run_export(**kwargs)
        _export_state["last_result"] = {
            "tickets": result.tickets,
            "comments": result.comments,
            "users": result.users,
            "resumed": result.resumed,
            "errors": len(result.errors),
        }
        log.info("Export finished: %s", _export_state["last_result"])
    except ConfigError as exc:
        # Expected before the Zendesk credentials are set - a stack trace here
        # would bury the one line that says what to do about it.
        _export_state["last_error"] = str(exc)
        log.error("Export not started: %s", exc)
    except Exception as exc:
        _export_state["last_error"] = str(exc)
        log.exception("Export failed: %s", exc)
    finally:
        _export_state["running"] = False
        _export_lock.release()


def _corpus_is_empty() -> bool:
    path = corpus_path()
    if not path.exists():
        return True
    with CorpusStore(path) as store:
        return store.stats()["tickets"] == 0


def kick_off_first_export() -> None:
    """Export the full history the first time the service boots.

    Deliberately conditional on an empty corpus: a redeploy should not re-walk
    1,300 tickets, and the volume keeps the corpus across deploys.
    """
    if os.environ.get("AUTO_EXPORT", "true").strip().lower() not in {"1", "true", "yes"}:
        log.info("AUTO_EXPORT disabled; not exporting on boot")
        return
    try:
        if not _corpus_is_empty():
            log.info("Corpus already populated; skipping the boot export")
            return
    except Exception as exc:
        log.warning("Could not inspect corpus (%s); skipping boot export", exc)
        return

    log.info("Corpus is empty - starting full Zendesk export in the background")
    threading.Thread(target=_run_export, kwargs={"full": True}, daemon=True).start()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    kick_off_first_export()
    yield


app = FastAPI(
    title="Evolution Golf support drafting",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Unauthenticated liveness check for Railway."""
    return {"status": "ok"}


@app.get("/stats", dependencies=[Depends(require_admin)])
def stats() -> dict[str, Any]:
    path = corpus_path()
    corpus = {"tickets": 0, "comments": 0, "public_comments": 0, "users": 0,
              "solved_tickets": 0} if not path.exists() else None
    if corpus is None:
        with CorpusStore(path) as store:
            corpus = store.stats()
    return {
        "corpus": corpus,
        "export_running": _export_state["running"],
        "last_export": _export_state["last_result"],
        "last_error": _export_state["last_error"],
    }


@app.post("/export", status_code=status.HTTP_202_ACCEPTED,
          dependencies=[Depends(require_admin)])
def trigger_export(full: bool = False) -> dict[str, str]:
    """Start an incremental export (or a full re-walk with ?full=true)."""
    if _export_state["running"]:
        return {"status": "already_running"}
    threading.Thread(target=_run_export, kwargs={"full": full}, daemon=True).start()
    return {"status": "started", "mode": "full" if full else "incremental"}


@app.get("/")
def root() -> Response:
    return Response(
        "Evolution Golf support drafting service. See /health.",
        media_type="text/plain",
    )
