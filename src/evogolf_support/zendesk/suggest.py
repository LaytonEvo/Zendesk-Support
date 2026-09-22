"""Put a suggested reply on a ticket as an internal note.

This is the delivery route for Zendesk plans that cannot upload a private
app - which is most of them below Growth. A trigger fires a webhook when a
customer writes in, this drafts the reply, and the draft lands in the
ticket as a private comment the customer never sees.

Every guard here exists for the same reason: a webhook that writes back to
the thing that fired it is a loop waiting to happen, and the cost of one
mistake is a customer-visible reply nobody reviewed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

from .. import slack
from ..config import load_dotenv
from ..corpus.store import CorpusStore
from ..drafting import shopify
from ..drafting.generate import Draft, draft_reply
from .client import ZendeskClient, ZendeskNotFound

log = logging.getLogger(__name__)

# One customer message gets one suggestion; this records which comment each
# ticket was last drafted for, so a redelivered webhook is a no-op.
SUGGESTED_KEY = "suggested_comments"
# Rolling per-hour count, so a misconfigured trigger cannot bill a thousand
# drafts before anyone notices. Same reasoning as the sweep's ticket ceiling.
HOURLY_KEY = "suggest_hourly"
HOURLY_CEILING = 40

NOTE_HEADER = "\U0001f9ed Suggested reply - draft only, not sent to the customer"
FOOTER = (
    "Generated automatically from past replies and settled policy. "
    "Check it, edit it, send it yourself. Nothing here has reached the customer."
)


def _state_json(store: CorpusStore, key: str) -> dict[str, Any]:
    raw = store.get_state(key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _within_hourly_ceiling(store: CorpusStore, now: dt.datetime) -> bool:
    """Count this draft against the current hour, refusing past the ceiling."""
    hour = now.strftime("%Y-%m-%dT%H")
    state = _state_json(store, HOURLY_KEY)
    used = int(state.get("count", 0)) if state.get("hour") == hour else 0
    if used >= HOURLY_CEILING:
        return False
    store.set_state(HOURLY_KEY, json.dumps({"hour": hour, "count": used + 1}))
    return True


_api_user_id: int | None = None


def api_user_id(client: ZendeskClient) -> int | None:
    """Who our API token authenticates as, looked up once per process.

    Needed on every webhook to recognise our own comments, and it does not
    change, so it is not worth an API call each time.
    """
    global _api_user_id
    if _api_user_id is None:
        try:
            _api_user_id = int(client.verify().get("id") or 0) or None
        except Exception as exc:                       # noqa: BLE001
            log.warning("Could not identify our own Zendesk user: %s", exc)
    return _api_user_id


def _already_suggested(store: CorpusStore, ticket_id: int) -> int | None:
    seen = _state_json(store, SUGGESTED_KEY)
    value = seen.get(str(ticket_id))
    return int(value) if isinstance(value, int) else None


def _record_suggested(store: CorpusStore, ticket_id: int, comment_id: int) -> None:
    seen = _state_json(store, SUGGESTED_KEY)
    seen[str(ticket_id)] = comment_id
    # Keep the most recent few thousand; this is a guard, not an archive.
    if len(seen) > 5000:
        for key in sorted(seen, key=lambda k: int(k))[: len(seen) - 5000]:
            seen.pop(key, None)
    store.set_state(SUGGESTED_KEY, json.dumps(seen))


def format_note(draft: Draft) -> str:
    """Render a draft as the note an agent reads.

    When the policy says a person should take the ticket, the draft is
    withheld rather than shown with a warning above it. A reply sitting in
    the thread is going to get used, and the whole point of the handover is
    that this one should not be.
    """
    if draft.hand_to_agent:
        reason = draft.handover_reason or "The settled policy says a person should handle this."
        return (
            "⚠️ Needs an agent - no draft written\n\n"
            f"{reason}\n\n"
            "This one is deliberately not drafted. Handle it yourself."
        )

    parts = [NOTE_HEADER, "", draft.draft.strip(), ""]
    if draft.agent_notes:
        parts.append("Check before sending:")
        parts.extend(f"  - {note}" for note in draft.agent_notes)
        parts.append("")
    meta = [f"confidence: {draft.confidence or '?'}"]
    if draft.rules_applied:
        meta.append("policy rules: " + ", ".join(str(r) for r in draft.rules_applied))
    if draft.tickets_referenced:
        meta.append("past tickets: " + ", ".join(str(t) for t in draft.tickets_referenced))
    parts.append(" | ".join(meta))
    parts.append(FOOTER)
    return "\n".join(parts)


def suggest_for_ticket(ticket_id: int, corpus: Any) -> str:
    """Draft a reply for one ticket and leave it as an internal note.

    Returns a short reason string, always - the caller is a webhook handler
    running in the background, so the log line is the only place an outcome
    is ever seen.
    """
    load_dotenv()
    if not corpus.exists():
        return "no corpus"

    with ZendeskClient() as client:
        try:
            ticket = client.ticket(ticket_id)
        except ZendeskNotFound:
            return "ticket gone"
        if ticket.get("status") in ("closed", "solved"):
            return "ticket already closed"

        comments = client.ticket_comments(ticket_id)
        if not comments:
            return "no comments"

        # Draft for the customer's most recent message, not for whatever
        # happens to be last in the thread.
        #
        # Reading comments[-1] and requiring it to be the requester's looked
        # like the tighter test and was in fact the weaker one. This account
        # runs an auto-acknowledgement trigger, which posts a public reply
        # within seconds of a ticket arriving - so by the time this handler
        # reads the thread, the customer's message is usually no longer last.
        # Every new email ticket would have been skipped, and skipped
        # silently, with a log line that read like a correct decision.
        me = api_user_id(client)
        requester = ticket.get("requester_id")
        mine = [c for c in comments
                if c.get("author_id") == requester and c.get("public", True)]
        if not mine:
            return "nothing from the requester to reply to"
        latest = mine[-1]

        # Have we already answered this message? The stored record below is
        # the usual answer, but the thread itself carries the same fact and
        # survives the corpus database being rebuilt, so it is asked first.
        after = comments[comments.index(latest) + 1:]
        if me and any(c.get("author_id") == me for c in after):
            return "already noted on this message"

        comment_id = int(latest.get("id") or 0)
        with CorpusStore(corpus) as store:
            if _already_suggested(store, ticket_id) == comment_id:
                return "already suggested for this comment"
            if not _within_hourly_ceiling(store, dt.datetime.now(dt.timezone.utc)):
                log.warning(
                    "Hourly suggestion ceiling of %s reached - refusing ticket %s. "
                    "Check the Zendesk trigger conditions.", HOURLY_CEILING, ticket_id
                )
                return "hourly ceiling reached"

            subject = ticket.get("subject") or ""
            body = latest.get("plain_body") or latest.get("body") or ""
            via = ticket.get("via") or {}
            email = ((via.get("source") or {}).get("from") or {}).get("address") or ""

            order_context = shopify.context_for_ticket(
                f"{subject} {body}", email=email or None, name=None
            ) or None
            draft = draft_reply(
                store, subject=subject, body=body, theme=None,
                order_context=order_context,
            )
            client.add_internal_note(ticket_id, format_note(draft))
            _record_suggested(store, ticket_id, comment_id)

    # After the note, never instead of it. Slack is where someone reads
    # this; the ticket is where it has to be. If Slack fails, the draft is
    # still on the ticket and the agent still has it.
    try:
        slack.post_draft(ticket_id, subject, body, draft)
    except Exception as exc:                            # noqa: BLE001
        log.warning("Draft posted to ticket %s but not to Slack: %s",
                    ticket_id, exc)

    log.info("Suggested a reply on ticket %s (handover=%s)",
             ticket_id, draft.hand_to_agent)
    return "handed to an agent" if draft.hand_to_agent else "suggested"


def webhook_token() -> str:
    """The shared secret the Zendesk webhook presents.

    Its own variable rather than ADMIN_TOKEN: this one is typed into Zendesk
    and seen by whoever administers it, so it should not also be the key to
    the export and reindex endpoints. Falls back to ADMIN_TOKEN only so the
    route works before the variable is set.
    """
    load_dotenv()
    return (os.environ.get("ZENDESK_WEBHOOK_TOKEN")
            or os.environ.get("ADMIN_TOKEN") or "").strip()
