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

from ..config import ConfigError, corpus_path, redact_pii
from ..corpus.quality import report as quality_report
from ..corpus.reclean import needs_reclean, reclean
from ..corpus.themes import report as theme_report
from ..corpus.store import CorpusStore
from ..zendesk.export import CURSOR_KEY

log = logging.getLogger(__name__)


def configure_logging() -> None:
    """Send our INFO logs to stdout so Railway shows them.

    Without a root handler, Python's last-resort handler emits WARNING and
    above only - which silently swallowed every progress line the export
    writes, leaving a 20-minute job looking like it had never started.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    # httpx logs a line per request; across a ~1,400-call export that buries
    # the progress lines that actually say how far along we are.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

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
            "deleted": result.deleted,
            "resumed": result.resumed,
            "errors": len(result.errors),
        }
        log.info("Export finished: %s", _export_state["last_result"])
        log_quality_report()
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


def reclean_if_rules_changed() -> None:
    """Apply newer cleaning rules to comments already exported.

    Cheap and local - it rebuilds clean_body from the raw bodies already in
    the corpus, so a cleaning fix does not mean re-exporting from Zendesk.
    """
    try:
        path = corpus_path()
        if not path.exists():
            return
        with CorpusStore(path) as store:
            if not needs_reclean(store):
                return
            log.info("Cleaning rules have changed - re-cleaning the corpus")
            reclean(store, redact_pii=redact_pii())
    except Exception as exc:
        log.warning("Could not re-clean the corpus: %s", exc)


def log_quality_report() -> None:
    """Log corpus coverage and cleaning stats - counts only, no content."""
    try:
        path = corpus_path()
        if not path.exists():
            return
        with CorpusStore(path) as store:
            for section, values in quality_report(store).items():
                log.info("quality/%s: %s", section, values)
            for section, values in theme_report(store).items():
                log.info("themes/%s: %s", section, values)
    except Exception as exc:
        log.warning("Could not build the quality report: %s", exc)


def _saved_cursor() -> str | None:
    """The export cursor from a previous run, if there is one."""
    path = corpus_path()
    if not path.exists():
        return None
    with CorpusStore(path) as store:
        return store.get_state(CURSOR_KEY)


def kick_off_first_export() -> None:
    """Bring the corpus up to date on every boot.

    Keyed on the saved cursor rather than on whether the corpus has rows.
    A first boot has no cursor and walks the full history; a boot after an
    interrupted export resumes from the cursor and finishes the job; a boot
    after a completed export costs one API call and fetches only what changed.

    Keying this on "is the corpus empty" instead would strand a part-finished
    export: the rows written so far would suppress the very run needed to
    complete it.
    """
    if os.environ.get("AUTO_EXPORT", "true").strip().lower() not in {"1", "true", "yes"}:
        log.info("AUTO_EXPORT disabled; not exporting on boot")
        return
    try:
        cursor = _saved_cursor()
    except Exception as exc:
        log.warning("Could not read the export cursor (%s); skipping boot export", exc)
        return

    if cursor:
        log.info("Resuming Zendesk export from the saved cursor")
    else:
        log.info("No saved cursor - starting a full Zendesk export in the background")
    threading.Thread(
        target=_run_export, kwargs={"full": cursor is None}, daemon=True
    ).start()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    reclean_if_rules_changed()
    log_quality_report()
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


@app.get("/quality", dependencies=[Depends(require_admin)])
def quality() -> dict[str, Any]:
    """Coverage and cleaning measurements. Counts only - no message content."""
    path = corpus_path()
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No corpus yet.")
    with CorpusStore(path) as store:
        return quality_report(store)


@app.get("/themes", dependencies=[Depends(require_admin)])
def themes() -> dict[str, Any]:
    """What the tickets are about, by volume. Aggregate counts only."""
    path = corpus_path()
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No corpus yet.")
    with CorpusStore(path) as store:
        return theme_report(store)


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
