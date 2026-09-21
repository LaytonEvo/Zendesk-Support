"""Export the full Zendesk ticket history into the local corpus.

The first run walks everything from ``start_time`` forward; later runs resume
from the saved cursor, so re-running is cheap and safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import corpus_path, redact_pii
from ..corpus.clean import clean_body
from ..corpus.store import CorpusStore
from .client import ZendeskClient

log = logging.getLogger(__name__)

CURSOR_KEY = "tickets_after_cursor"
# Zendesk's oldest allowed start_time is the epoch; 2015 comfortably predates
# any Evolution Golf ticket while keeping the first page small.
DEFAULT_START_TIME = 1420070400  # 2015-01-01


@dataclass
class ExportResult:
    tickets: int = 0
    comments: int = 0
    users: int = 0
    resumed: bool = False
    errors: list[str] = field(default_factory=list)


def run_export(
    *,
    start_time: int = DEFAULT_START_TIME,
    full: bool = False,
    limit: int | None = None,
) -> ExportResult:
    """Pull tickets and their comments into the corpus.

    Args:
        start_time: Unix timestamp to export from on a first run.
        full: Ignore the saved cursor and re-walk from ``start_time``.
        limit: Stop after this many tickets (useful for a trial run).
    """
    result = ExportResult()
    redact = redact_pii()

    with ZendeskClient() as client, CorpusStore(corpus_path()) as store:
        who = client.verify()
        log.info(
            "Authenticated to Zendesk as %s (%s)",
            who.get("name", "?"),
            who.get("role", "?"),
        )

        cursor = None if full else store.get_state(CURSOR_KEY)
        result.resumed = cursor is not None

        seen_user_ids: set[int] = set()
        stop = False

        for page in client.incremental_tickets(start_time, cursor):
            for ticket in page.get("tickets", []):
                ticket_id = ticket.get("id")
                if ticket_id is None:
                    continue

                store.upsert_ticket(ticket)
                result.tickets += 1

                try:
                    comments = client.ticket_comments(ticket_id)
                except Exception as exc:  # keep going; one bad ticket is not fatal
                    msg = f"ticket {ticket_id}: {exc}"
                    log.warning("Could not fetch comments for %s", msg)
                    result.errors.append(msg)
                    comments = []

                for comment in comments:
                    comment["clean_body"] = clean_body(
                        comment.get("plain_body") or comment.get("body") or "",
                        redact_pii=redact,
                    )
                    if comment.get("author_id"):
                        seen_user_ids.add(int(comment["author_id"]))

                store.replace_comments(ticket_id, comments)
                result.comments += len(comments)

                if result.tickets % 50 == 0:
                    log.info("… %s tickets, %s comments", result.tickets, result.comments)

                if limit is not None and result.tickets >= limit:
                    stop = True
                    break

            # Save the cursor per page so an interrupted run resumes cleanly.
            after = page.get("after_cursor")
            if after and not stop:
                store.set_state(CURSOR_KEY, after)

            if stop:
                break

        if seen_user_ids:
            users = client.users(sorted(seen_user_ids))
            store.upsert_users(users)
            result.users = len(users)

        log.info("Corpus now holds: %s", store.stats())

    return result
