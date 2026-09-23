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

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from typing import Any, AsyncIterator

from fastapi import (
    BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status,
)
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from html import escape as html_escape

from pydantic import BaseModel

from ..config import ConfigError, corpus_path, redact_pii
from ..corpus.evidence import threads_for
from ..corpus.quality import report as quality_report
from ..corpus.reclean import needs_reclean, reclean
from ..corpus.themes import report as theme_report
from ..mining.discover import Taxonomy, classify_tickets, discover_themes
from ..mining.run import GUIDE_KEY, run_mining
from ..drafting.generate import Draft, draft_reply
from ..drafting.retrieve import rebuild_index
from ..drafting import shopify, shopify_oauth
from ..drafting.evaluate import run_evaluation
from ..proactive.run import run_sweep
from ..corpus.store import CorpusStore
from ..zendesk.export import CURSOR_KEY

# Set once the online@ history has been pulled in, so it is not re-imported
# on every boot.
GMAIL_IMPORTED_KEY = "gmail_history_imported"
from .. import slack
from ..zendesk import suggest
from ..gmail import ingest as gmail_ingest, oauth as google_oauth
from .. import metrics
from . import dashboard

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
    # Two endpoints have to take the admin token in the query string, because
    # a browser cannot send a header from the address bar. Uvicorn's access
    # log writes the full request line, so without this the token is printed
    # in plain text into a log anyone with dashboard access can read - and
    # into whatever ships those logs onward. Redact it at the source.
    logging.getLogger("uvicorn.access").addFilter(_RedactQueryToken())


class _RedactQueryToken(logging.Filter):
    """Strip a `token=` query parameter out of anything logged."""

    _PATTERN = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._PATTERN.sub(r"\1REDACTED", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1REDACTED", record.msg)
        return True

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
        rebuild_search_index()
        log_quality_report()
        log_inbound_addresses()
        # After the export, so it judges against fresh ticket state.
        catch_up_suggestions()
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


TAXONOMY_KEY = "taxonomy"

_mine_lock = threading.Lock()
_mine_state: dict[str, Any] = {"running": False, "error": None}


def _run_discovery() -> None:
    """Discover the theme taxonomy and classify every ticket.

    Costs a handful of Opus calls, so it runs once and the result is stored.
    """
    if not _mine_lock.acquire(blocking=False):
        return
    _mine_state.update(running=True, error=None)
    try:
        with CorpusStore(corpus_path()) as store:
            taxonomy = discover_themes(store)
            store.set_state(TAXONOMY_KEY, taxonomy.model_dump_json())
            assignments = classify_tickets(store, taxonomy)
            store.set_ticket_themes(assignments)
            log.info("discovered/notes: %s", taxonomy.notes)
            for theme in taxonomy.themes:
                log.info("discovered/theme %s: %s", theme.key, theme.definition)
            log.info("discovered/counts: %s", store.theme_counts())
            log.info("discovered/agent_replies: %s", store.theme_agent_replies())
            run_mining(store, taxonomy)
    except ConfigError as exc:
        _mine_state["error"] = str(exc)
        log.error("Theme discovery not started: %s", exc)
    except Exception as exc:
        _mine_state["error"] = str(exc)
        log.exception("Theme discovery failed: %s", exc)
    finally:
        _mine_state["running"] = False
        _mine_lock.release()


def _resume_mining(taxonomy: Taxonomy) -> None:
    if not _mine_lock.acquire(blocking=False):
        return
    _mine_state.update(running=True, error=None)
    try:
        with CorpusStore(corpus_path()) as store:
            run_mining(store, taxonomy)
    except ConfigError as exc:
        _mine_state["error"] = str(exc)
        log.error("Mining not started: %s", exc)
    except Exception as exc:
        _mine_state["error"] = str(exc)
        log.exception("Mining failed: %s", exc)
    finally:
        _mine_state["running"] = False
        _mine_lock.release()


def kick_off_discovery() -> None:
    """Run theme discovery once, when the corpus has tickets but no taxonomy."""
    if os.environ.get("AUTO_MINE", "true").strip().lower() not in {"1", "true", "yes"}:
        log.info("AUTO_MINE disabled; not running theme discovery")
        return
    try:
        path = corpus_path()
        if not path.exists():
            return
        with CorpusStore(path) as store:
            if store.get_state(GUIDE_KEY):
                log.info("Voice guide already stored; nothing to mine")
                return
            if store.stats()["tickets"] == 0:
                return
            if store.get_state(TAXONOMY_KEY):
                # Discovery already ran; resume at mining rather than redoing it.
                log.info("Taxonomy stored but no guide - resuming at mining")
                taxonomy = Taxonomy.model_validate_json(store.get_state(TAXONOMY_KEY))
                threading.Thread(
                    target=_resume_mining, args=(taxonomy,), daemon=True
                ).start()
                return
    except Exception as exc:
        log.warning("Could not check for a stored taxonomy: %s", exc)
        return

    log.info("No taxonomy stored - discovering themes from the corpus")
    threading.Thread(target=_run_discovery, daemon=True).start()


def log_requested_evidence() -> None:
    """Log full threads for the tickets named in EVIDENCE_TICKETS.

    Used to assemble the evidence pack that sits behind each open decision in
    the voice guide. Logging keeps the corpus behind the service's existing
    auth rather than opening a public endpoint for it.
    """
    raw = os.environ.get("EVIDENCE_TICKETS", "").strip()
    if not raw:
        return
    try:
        ids = [int(part) for part in raw.replace(" ", "").split(",") if part]
    except ValueError:
        log.warning("EVIDENCE_TICKETS is not a comma-separated list of ids")
        return
    try:
        path = corpus_path()
        if not path.exists():
            return
        with CorpusStore(path) as store:
            for thread in threads_for(store, ids):
                log.info("evidence/%s: %s", thread["id"], json.dumps(thread))
        log.info("evidence/done: %s tickets", len(ids))
    except Exception as exc:
        log.warning("Could not assemble evidence: %s", exc)


def log_integration_status() -> None:
    """Say plainly which integrations are live.

    Silence is ambiguous: a mistyped variable name and a working lookup that
    simply found no order produce the same empty result. This makes the
    difference visible on every boot.
    """
    if shopify.configured():
        log.info("Shopify order lookup: configured")
    else:
        log.warning(
            "Shopify order lookup: NOT configured - drafts will leave order "
            "status, tracking and figures as placeholders. Set "
            "SHOPIFY_STORE_DOMAIN plus SHOPIFY_CLIENT_ID and "
            "SHOPIFY_CLIENT_SECRET (or a legacy SHOPIFY_ACCESS_TOKEN)."
        )

    if os.environ.get("ZENDESK_WEBHOOK_TOKEN", "").strip():
        log.info("Ticket webhook: configured - drafts will post as internal notes")
    elif _admin_token():
        log.warning(
            "Ticket webhook: falling back to ADMIN_TOKEN. Set "
            "ZENDESK_WEBHOOK_TOKEN to its own value so the token typed into "
            "Zendesk does not also unlock /export and /reindex."
        )
    else:
        log.warning("Ticket webhook: NOT configured - no drafts will be posted.")

    if google_oauth.configured():
        log.info("Gmail import: credentials set")
    else:
        log.info(
            "Gmail import: not configured - the online@ history stays out of "
            "the corpus. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
        )

    if slack.configured():
        log.info("Slack: configured - drafts and the delay digest will be posted")
    else:
        log.warning(
            "Slack: NOT configured - drafts go to the ticket only. Set "
            "SLACK_WEBHOOK_URL to have them posted where someone will see them."
        )


def rebuild_search_index() -> None:
    """Keep retrieval in step with the corpus. Cheap - no API calls."""
    try:
        path = corpus_path()
        if not path.exists():
            return
        with CorpusStore(path) as store:
            rebuild_index(store)
    except Exception as exc:
        log.warning("Could not rebuild the search index: %s", exc)


def run_requested_evaluation() -> None:
    """Draft against real solved tickets when DRAFT_SAMPLE is set.

    The only honest check of the system: would this draft have been a
    reasonable reply to a ticket the team already answered?
    """
    raw = os.environ.get("DRAFT_SAMPLE", "").strip()
    if not raw:
        return
    try:
        limit = int(raw)
    except ValueError:
        log.warning("DRAFT_SAMPLE must be a number")
        return
    theme = os.environ.get("DRAFT_SAMPLE_THEME", "").strip() or None

    def worker() -> None:
        try:
            with CorpusStore(corpus_path()) as store:
                run_evaluation(store, limit, theme)
        except Exception as exc:
            log.warning("Evaluation failed: %s", exc)

    threading.Thread(target=worker, daemon=True).start()


# How far back the catch-up looks, and how many tickets it will draft in one
# pass. Bounded because it runs unattended: a ceiling turns a bad day into a
# handful of extra notes rather than a hundred.
CATCH_UP_HOURS = 24
CATCH_UP_MAX = 10


def catch_up_suggestions() -> None:
    """Draft for recent tickets that never got a suggestion.

    Zendesk delivers a webhook once. Anything that arrives while the service
    is restarting, or while its credentials are being rejected, is simply
    lost - and the ticket sits there looking like the assistant considered it
    and declined. Four tickets went that way in one morning.

    Nothing here needs its own guards: suggest_for_ticket already refuses a
    ticket it has answered, one with no customer message, and one that is
    solved. This only decides which tickets to offer it.
    """
    try:
        path = corpus_path()
        if not path.exists():
            return
        since = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=CATCH_UP_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with CorpusStore(path) as store:
            candidates = store.recent_open_tickets(since, limit=CATCH_UP_MAX * 5)
    except Exception as exc:
        log.warning("Could not look for tickets needing a catch-up: %s", exc)
        return

    drafted = 0
    for ticket_id in candidates:
        if drafted >= CATCH_UP_MAX:
            log.info("catch-up: stopped at the %s-ticket ceiling", CATCH_UP_MAX)
            break
        try:
            outcome = suggest.suggest_for_ticket(ticket_id, corpus_path())
        except Exception as exc:
            log.warning("catch-up/ticket %s failed: %s", ticket_id, exc)
            continue
        if outcome in ("suggested", "handed to an agent"):
            drafted += 1
            log.info("catch-up/ticket %s: %s (missed webhook)", ticket_id, outcome)
    log.info("catch-up: %s ticket(s) drafted that had been missed", drafted)


# How often the corpus is refreshed from Zendesk. Without this the dashboard
# would only be as current as the last deploy, which is the kind of stale
# number people stop trusting and then stop looking at.
REFRESH_MINUTES = 30


def start_periodic_refresh() -> None:
    """Keep the corpus - and so the dashboard - current between deploys.

    The export is cursor-based and incremental, so a run with nothing new
    costs one API call. Failures are logged and the loop continues: a
    Zendesk outage should pause the numbers, not stop them updating for
    good once it is over.
    """
    def loop() -> None:
        while True:
            time.sleep(REFRESH_MINUTES * 60)
            try:
                _run_export(resume=True)
            except Exception as exc:                    # noqa: BLE001
                log.warning("Periodic refresh failed: %s", exc)

    threading.Thread(target=loop, daemon=True).start()
    log.info("Corpus will refresh from Zendesk every %s minutes", REFRESH_MINUTES)


def kick_off_gmail_import() -> None:
    """Import the online@ history once, on the first boot after authorising.

    A POST is unreachable from a browser and this account is run from one, so
    the import runs itself rather than waiting to be called. It is keyed on a
    stored marker, not on the corpus being empty: a run interrupted halfway
    would otherwise never resume.
    """
    try:
        path = corpus_path()
        if not path.exists():
            return
        with CorpusStore(path) as store:
            if not google_oauth.authorised(store):
                return
            if store.get_state(GMAIL_IMPORTED_KEY):
                log.info("Gmail history already imported; skipping")
                return
    except Exception as exc:
        log.warning("Could not check the Gmail import state: %s", exc)
        return

    def worker() -> None:
        try:
            with CorpusStore(corpus_path()) as store:
                result = gmail_ingest.import_threads(store)
                store.set_state(GMAIL_IMPORTED_KEY, json.dumps(result))
                rebuild_index(store)
            log.info("Gmail import finished: %s", result)
        except Exception as exc:
            log.warning("Gmail import failed: %s", exc)

    log.info("Importing the online@ history into the corpus")
    threading.Thread(target=worker, daemon=True).start()


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


def log_inbound_addresses() -> None:
    """Which addresses tickets actually arrive on, and how recently.

    A test email that never became a ticket looks the same whether the
    address is not connected to Zendesk at all or the mail simply has not
    landed yet. The exported history settles it: whatever address customers
    have been writing to for the past year is the one that works.
    """
    try:
        path = corpus_path()
        if not path.exists():
            return
        seen: dict[str, dict[str, Any]] = {}
        with CorpusStore(path) as store:
            for row in store.tickets_raw():
                try:
                    ticket = json.loads(row["raw"] or "{}")
                except json.JSONDecodeError:
                    continue
                to = (ticket.get("recipient") or "").strip().lower()
                if not to:
                    continue
                entry = seen.setdefault(to, {"tickets": 0, "latest": ""})
                entry["tickets"] += 1
                created = ticket.get("created_at") or ""
                if created > entry["latest"]:
                    entry["latest"] = created
        log.info("channels/inbound_addresses: %s", seen or "none recorded")
    except Exception as exc:
        log.warning("Could not summarise inbound addresses: %s", exc)


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
    log_integration_status()
    reclean_if_rules_changed()
    rebuild_search_index()
    log_quality_report()
    log_inbound_addresses()
    log_requested_evidence()
    run_requested_evaluation()
    kick_off_first_export()
    kick_off_gmail_import()
    kick_off_discovery()
    start_periodic_refresh()
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


class DraftRequest(BaseModel):
    subject: str = ""
    body: str
    theme: str | None = None
    order_context: str | None = None
    requester_email: str | None = None
    requester_name: str | None = None
    lookup_order: bool = True


@app.post("/draft", dependencies=[Depends(require_admin)])
def draft(req: DraftRequest) -> Draft:
    """Draft a reply for a ticket. Never sends; an agent reviews every draft."""
    path = corpus_path()
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No corpus yet.")
    if not req.body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is required.")
    order_context = req.order_context
    if order_context is None and req.lookup_order:
        # Best effort: a lookup failure must not stop the draft, it just
        # means the agent fills the figures in instead.
        order_context = shopify.context_for_ticket(
            f"{req.subject} {req.body}",
            email=req.requester_email,
            name=req.requester_name,
        ) or None

    with CorpusStore(path) as store:
        return draft_reply(
            store,
            subject=req.subject,
            body=req.body,
            theme=req.theme,
            order_context=order_context,
        )


class TicketHook(BaseModel):
    """What the Zendesk trigger sends. Only the id is needed.

    The trigger's placeholder is {{ticket.id}}, which Zendesk renders as a
    string, so the field is parsed permissively rather than requiring an int.
    """

    ticket_id: int


@app.post("/zendesk/hook", status_code=status.HTTP_202_ACCEPTED)
def zendesk_hook(
    hook: TicketHook, tasks: BackgroundTasks, request: Request,
) -> dict[str, str]:
    """Draft a reply for a ticket and leave it as an internal note.

    Answers immediately and works in the background. Drafting takes longer
    than Zendesk is willing to wait for a webhook, and a timeout there means
    a retry - which would draft the same ticket twice.
    """
    expected = suggest.webhook_token()
    if not expected:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "ZENDESK_WEBHOOK_TOKEN is not set, so this endpoint is disabled.",
        )
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token.")

    tasks.add_task(_run_suggestion, hook.ticket_id)
    return {"status": "accepted"}


def _run_suggestion(ticket_id: int) -> None:
    """Background half of the webhook. Nothing here may raise.

    An unhandled exception in a background task is invisible: the webhook has
    already answered 202 and Zendesk will never retry it, so a failure that
    is not logged is a ticket that silently never gets a draft.
    """
    try:
        outcome = suggest.suggest_for_ticket(ticket_id, corpus_path())
        log.info("hook/ticket %s: %s", ticket_id, outcome)
    except Exception as exc:                            # noqa: BLE001
        log.exception("hook/ticket %s failed: %s", ticket_id, exc)


@app.get("/proactive/preview")
def proactive_preview(
    request: Request,
    token: str = "",
    unfulfilled_days: int | None = None,
    transit_days: int | None = None,
) -> Response:
    """Show what the delay sweep would flag, as a readable page.

    Deliberately GET-and-dry-run-only. A URL can be re-requested by a browser,
    a prefetcher or a bookmark, so the one that is reachable that way must
    never be able to raise a ticket. The real sweep stays a POST.
    """
    admin = _admin_token()
    if not admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "ADMIN_TOKEN is not set, so this endpoint is disabled.",
        )
    header = request.headers.get("authorization", "")
    supplied = token or (header[7:] if header.lower().startswith("bearer ") else "")
    if not supplied or not secrets.compare_digest(supplied, admin):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin token.")

    from ..proactive.detect import (
        fetch_orders, find_at_risk, summarise,
    )

    try:
        # One crawl of the shop, read twice: what would be flagged, and what
        # was looked at. Showing only the first without the second is how a
        # detector that never matches anything passes for a quiet week.
        orders = fetch_orders()
        summary = summarise(orders)
        at_risk = find_at_risk(
            orders=orders,
            unfulfilled_days=unfulfilled_days, transit_days=transit_days,
        )
    except Exception as exc:
        log.exception("Delay preview failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not read orders.")

    def _counts(d: dict[str, int]) -> str:
        return ", ".join(f"{k} x{v}" for k, v in sorted(d.items())) or "none"

    courier = summary.get("courier", {})
    status_line = _counts(courier)
    state_line = _counts(summary.get("fulfilment", {}))
    scanned = summary.get("orders", 0)
    undispatched = summary.get("no_fulfilment", 0)
    window = f"{summary.get('oldest') or '?'} to {summary.get('newest') or '?'}"
    carriers = summary.get("carriers", {})
    blind = summary.get("blind_carriers", [])
    carrier_line = ", ".join(
        f"{name} {c['confirmed']}/{c['shipped']} confirmed delivered"
        for name, c in sorted(carriers.items())
    ) or "none"
    feed_note = (
        "" if not blind else
        "No delivery confirmation has ever arrived from " + ", ".join(blind)
        + ". Parcels sent this way cannot be checked for slow delivery at all "
        "\u2014 a lost one looks exactly like a delivered one. Only a customer "
        "writing in will surface those. Carriers that do confirm are still "
        "checked, as are undispatched orders and reported failures."
    )

    rows = "".join(
        f"<tr><td>{html_escape(i.order_name)}</td>"
        f"<td>{html_escape(i.customer_name or '-')}</td>"
        f"<td>{html_escape(i.reason.replace('_', ' '))}</td>"
        f"<td>{html_escape(i.detail)}</td>"
        f"<td>{html_escape(i.items[:70])}</td></tr>"
        for i in at_risk
    )
    body = f"""<!doctype html><meta charset="utf-8">
<title>Delay sweep preview</title>
<style>
 body{{font:15px/1.5 system-ui,sans-serif;margin:0;padding:28px 20px;color:#1a2b30;background:#f4f6f6}}
 .card{{max-width:1000px;margin:0 auto;background:#fff;border:1px solid #d8dcdd;border-radius:8px;padding:22px}}
 h1{{font-size:21px;margin:0 0 4px}} p{{color:#55686e;margin:0 0 18px}}
 table{{border-collapse:collapse;width:100%;font-size:13.5px}}
 th{{text-align:left;font-size:11px;letter-spacing:.06em;text-transform:uppercase;
     color:#74898f;border-bottom:1px solid #d8dcdd;padding:8px 10px}}
 td{{padding:9px 10px;border-bottom:1px solid #eceff0;vertical-align:top}}
 .none{{padding:26px;text-align:center;color:#55686e}}
 .note{{margin-top:18px;font-size:13px;color:#74898f}}
</style>
<div class="card">
<h1>Delay sweep preview</h1>
<p><strong>{len(at_risk)}</strong> order(s) would be flagged. Nothing has been
created and no customer has been contacted.</p>
{'<table><tr><th>Order</th><th>Customer</th><th>Reason</th><th>Detail</th><th>Items</th></tr>'
 + rows + '</table>' if at_risk else '<div class="none">Nothing is running late.</div>'}
<div class="note">Thresholds in use: undispatched beyond
<strong>{unfulfilled_days if unfulfilled_days is not None else 3}</strong> working days,
in transit beyond <strong>{transit_days if transit_days is not None else 5}</strong>,
plus any courier-reported failure. Weekends excluded.<br><br>
Looked at <strong>{scanned}</strong> paid order(s), {html_escape(window)},
of which <strong>{undispatched}</strong> have nothing dispatched at all.<br>
Order states: <strong>{html_escape(state_line)}</strong>.<br>
Courier statuses: <strong>{html_escape(status_line)}</strong>.<br>
By carrier: <strong>{html_escape(carrier_line)}</strong>.<br>
{html_escape(feed_note)}</div>
</div>"""
    return Response(body, media_type="text/html")


@app.post("/proactive/sweep", dependencies=[Depends(require_admin)])
def proactive_sweep(dry_run: bool = False) -> dict[str, int]:
    """Look for delayed orders and raise a ticket with a drafted message.

    Nothing reaches a customer: the draft goes on as an internal note.
    Intended to run once a day from a scheduler.
    """
    return run_sweep(dry_run=dry_run)


@app.post("/reindex", dependencies=[Depends(require_admin)])
def reindex() -> dict[str, int]:
    with CorpusStore(corpus_path()) as store:
        return {"indexed": rebuild_index(store)}


def _check_admin_query(request: Request, token: str) -> None:
    """Admin check for an endpoint a browser must be able to open.

    A browser cannot send an Authorization header from the address bar, so
    the token is also accepted as a query parameter. That puts it in browser
    history, which is why these responses carry Referrer-Policy: no-referrer
    and why ADMIN_TOKEN should be rotated afterwards.
    """
    admin = _admin_token()
    if not admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "ADMIN_TOKEN is not set, so this endpoint is disabled.",
        )
    header = request.headers.get("authorization", "")
    supplied = token or (header[7:] if header.lower().startswith("bearer ") else "")
    if not supplied or not secrets.compare_digest(supplied, admin):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin token.")


SESSION_COOKIE = "evo_dash"
# Long enough to cover a working day without a second sign-in, short enough
# that a laptop left open in the shop does not stay signed in for a week.
SESSION_HOURS = 12


def _dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "").strip()


def _dashboard_token() -> str:
    """For scripts, not for people. The browser path is the password."""
    return os.environ.get("DASHBOARD_TOKEN", "").strip()


def _sign(expires: int) -> str:
    """A session cookie signed with the password itself.

    Nothing about the password is recoverable from it, and changing the
    password invalidates every issued session - which is the behaviour you
    want from the only lever available when someone leaves.
    """
    key = _dashboard_password().encode()
    digest = hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{digest}"


def _valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie or not _dashboard_password():
        return False
    raw, _, digest = cookie.partition(".")
    try:
        expires = int(raw)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    expected = _sign(expires).partition(".")[2]
    return hmac.compare_digest(expected, digest)


def _signed_in(request: Request) -> bool:
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return True
    # A bearer token still works for anything scripted.
    token = _dashboard_token()
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    return bool(token and supplied and secrets.compare_digest(supplied, token))


@app.post("/dashboard/login")
async def dashboard_login(request: Request) -> Response:
    expected = _dashboard_password()
    if not expected:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "DASHBOARD_PASSWORD is not set, so the dashboard is disabled.",
        )
    # Parsed directly rather than through request.form(), which pulls in a
    # multipart dependency for what is one urlencoded field.
    body = (await request.body()).decode("utf-8", "replace")
    supplied = parse_qs(body).get("password", [""])[0]
    if not secrets.compare_digest(supplied, expected):
        log.warning("Dashboard sign-in refused")
        return Response(dashboard.login_page("That password is not right."),
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        media_type="text/html")

    expires = int(time.time()) + SESSION_HOURS * 3600
    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    # Secure for every real host. Only a local or test host - where there is
    # no TLS to require - gets a cookie without it, so production can never
    # accidentally issue one that travels in clear.
    local = (request.url.hostname or "") in ("testserver", "localhost", "127.0.0.1")
    response.set_cookie(
        SESSION_COOKIE, _sign(expires), max_age=SESSION_HOURS * 3600,
        httponly=True, secure=not local, samesite="lax", path="/dashboard",
    )
    return response


@app.get("/dashboard/logout")
def dashboard_logout() -> Response:
    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/dashboard")
    return response


@app.get("/dashboard")
def admin_dashboard(request: Request, range: str = "last30",
                    start: str = "", end: str = "") -> Response:
    """Read-only view of whether Zendesk is used and the drafts relied on."""
    if not _dashboard_password() and not _dashboard_token():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "DASHBOARD_PASSWORD is not set, so the dashboard is disabled.",
        )
    if not _signed_in(request):
        return Response(dashboard.login_page(), status_code=status.HTTP_401_UNAUTHORIZED,
                        media_type="text/html")

    path = corpus_path()
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No corpus yet.")
    window = metrics.resolve_range(range, start, end)
    try:
        with CorpusStore(path) as store:
            report = metrics.report(store, window)
    except Exception as exc:
        log.exception("Dashboard failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not build the report.")
    return Response(
        dashboard.render(report),
        media_type="text/html",
        headers={"Referrer-Policy": "no-referrer",
                 "Cache-Control": "no-store"},
    )


@app.get("/google/install")
def google_install(request: Request, token: str = "") -> RedirectResponse:
    """Start the one-off authorisation of the online@ mailbox."""
    _check_admin_query(request, token)
    if not google_oauth.configured():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.",
        )
    redirect_uri = str(request.url_for("google_callback")).replace("http://", "https://")
    log.info("Starting Google authorisation, redirecting to Google")
    return RedirectResponse(
        google_oauth.install_url(redirect_uri, google_oauth.new_nonce()),
        headers={"Referrer-Policy": "no-referrer"},
    )


@app.get("/google/callback", name="google_callback")
def google_callback(request: Request) -> Response:
    """Google's redirect once the mailbox owner approves read access."""
    params = dict(request.query_params)
    if params.get("error"):
        log.warning("Google authorisation was declined: %s", params["error"])
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Authorisation declined.")
    if not google_oauth.consume_nonce(params.get("state", "")):
        log.warning("Google callback rejected: unknown or reused state")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid request.")
    code = params.get("code")
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid request.")

    redirect_uri = str(request.url_for("google_callback")).replace("http://", "https://")
    try:
        payload = google_oauth.exchange_code(code, redirect_uri)
    except Exception as exc:
        log.exception("Google token exchange failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not reach Google.")

    refresh = payload.get("refresh_token")
    if not refresh:
        # Google only returns one on first consent. Without it the import
        # would work today and quietly stop when the access token expires.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google returned no refresh token. Remove this app at "
            "myaccount.google.com/permissions and authorise again.",
        )
    with CorpusStore(corpus_path()) as store:
        google_oauth.store_refresh_token(store, refresh)
    log.info("Gmail authorised - the online@ history can now be imported")
    return Response(
        "Gmail is connected, read-only. Now call /gmail/import to pull the "
        "labelled customer history into the corpus.",
        media_type="text/plain",
        headers={"Referrer-Policy": "no-referrer"},
    )


@app.post("/gmail/import", dependencies=[Depends(require_admin)])
def gmail_import(limit: int | None = None) -> dict[str, int]:
    """Import the labelled online@ conversations into the corpus.

    Safe to re-run: a conversation's id is derived from its Gmail thread, so
    a second run updates rather than duplicates.
    """
    with CorpusStore(corpus_path()) as store:
        result = gmail_ingest.import_threads(store, limit=limit)
        rebuild_index(store)
    return result


@app.get("/shopify/install")
def shopify_install(request: Request, token: str = "") -> RedirectResponse:
    """Start the one-off install of the app on the store.

    A browser cannot send an Authorization header from the address bar, and
    the Shopify approval screen needs a browser, so this endpoint also accepts
    the admin token as a query parameter. That puts it in browser history, so
    the response carries Referrer-Policy: no-referrer to keep it out of the
    Referer header on the hop to Shopify - and the token should be rotated
    once the install is done.
    """
    admin = _admin_token()
    if not admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "ADMIN_TOKEN is not set, so this endpoint is disabled.",
        )
    header = request.headers.get("authorization", "")
    supplied = token or (header[7:] if header.lower().startswith("bearer ") else "")
    if not supplied or not secrets.compare_digest(supplied, admin):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin token.")

    shop = shopify_oauth.expected_shop()
    if not shopify_oauth.valid_shop(shop):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "SHOPIFY_STORE_DOMAIN must be a myshopify.com domain.",
        )
    redirect_uri = str(request.url_for("shopify_callback")).replace("http://", "https://")
    nonce = shopify_oauth.new_nonce()
    log.info("Starting Shopify install for %s, redirecting to Shopify", shop)
    return RedirectResponse(
        shopify_oauth.install_url(shop, redirect_uri, nonce),
        headers={"Referrer-Policy": "no-referrer"},
    )


@app.get("/shopify/callback", name="shopify_callback")
def shopify_callback(request: Request) -> Response:
    """Shopify's redirect after the merchant approves the app.

    Shopify cannot present our admin token, so the signature and the
    single-use nonce are the only things standing between this endpoint and
    the internet. Every check is mandatory and failures say nothing useful
    to a caller.
    """
    params = dict(request.query_params)
    shop = (params.get("shop") or "").lower()

    if not shopify_oauth.valid_shop(shop) or shop != shopify_oauth.expected_shop():
        log.warning("Shopify callback rejected: unexpected shop %r", shop)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid request.")
    if not shopify_oauth.verify_hmac(params):
        log.warning("Shopify callback rejected: signature did not verify")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid request.")
    if not shopify_oauth.consume_nonce(params.get("state", "")):
        log.warning("Shopify callback rejected: unknown or reused state")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid request.")
    code = params.get("code")
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid request.")

    try:
        payload = shopify_oauth.exchange_code(shop, code)
    except Exception as exc:
        log.exception("Shopify code exchange failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not complete the install.")

    with CorpusStore(corpus_path()) as store:
        shopify_oauth.store_token(store, payload["access_token"])

    scopes = payload.get("scope", "")
    log.info("Shopify app installed on %s; granted scopes: %s", shop, scopes)
    if "read_all_orders" not in scopes:
        log.warning(
            "read_all_orders was NOT granted: lookups will return nothing for "
            "orders older than 60 days."
        )
    return Response(
        "Evolution Golf drafting service is now connected to Shopify. "
        "You can close this tab.",
        media_type="text/plain",
    )


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
