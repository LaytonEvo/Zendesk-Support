"""Turn an at-risk order into something an agent can act on.

A ticket is raised carrying a drafted message, and a digest summarises the
sweep. Nothing is sent to a customer: the draft goes on as an internal note
for an agent to review, edit and send themselves.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from ..config import load_dotenv
from ..corpus.store import CorpusStore
from ..drafting.generate import Draft, draft_reply
from ..zendesk.client import ZendeskClient
from .detect import AtRisk

log = logging.getLogger(__name__)

FLAGGED_KEY = "proactive_flagged"

REASON_BRIEF = {
    "not_dispatched":
        "This order is paid for and has not been dispatched yet. The customer "
        "has NOT contacted us - we are reaching out first, as policy requires.",
    "stuck_in_transit":
        "This order shipped but has not been delivered and is overdue. The "
        "customer has NOT contacted us - we are reaching out first.",
    "delivery_problem":
        "The courier has reported a problem with this delivery. The customer "
        "has NOT contacted us - we are reaching out first, and may well be "
        "telling them something they do not yet know.",
}


def already_flagged(store: CorpusStore) -> set[str]:
    raw = store.get_state(FLAGGED_KEY)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except json.JSONDecodeError:
        return set()


def mark_flagged(store: CorpusStore, keys: set[str]) -> None:
    """Remember what we have raised, so a customer is contacted once."""
    store.set_state(FLAGGED_KEY, json.dumps(sorted(keys)))


def flag_key(item: AtRisk) -> str:
    return f"{item.order_name}:{item.reason}"


def draft_for(store: CorpusStore, item: AtRisk) -> Draft:
    """Draft the proactive message, using the same policy and voice."""
    body = (
        f"{REASON_BRIEF.get(item.reason, '')}\n\n"
        f"Order {item.order_name} for {item.customer_name or 'the customer'}.\n"
        f"Items: {item.items or 'not listed'}\n"
        f"Situation: {item.detail}."
        + (f"\nTracking: {item.tracking}" if item.tracking else "")
        + "\n\nWrite the message we send them now: say what has happened, why, "
        "and what we are doing about it. Do not wait to be asked and do not "
        "apologise for a fault that has not been established."
    )
    return draft_reply(
        store,
        subject=f"Proactive update on order {item.order_name}",
        body=body,
        theme="order_status_delivery",
        order_context=(
            f"Order {item.order_name}\n{item.detail}\n"
            f"Items: {item.items}\n"
            + (f"Tracking: {item.tracking}" if item.tracking else "No tracking recorded")
        ),
    )


def _set_requester() -> bool:
    load_dotenv()
    return os.environ.get("PROACTIVE_SET_REQUESTER", "").strip().lower() in {
        "1", "true", "yes"
    }


def raise_ticket(item: AtRisk, draft: Draft) -> int | None:
    """Raise an internal ticket carrying the draft.

    By default the ticket has no customer requester. A ticket created with a
    requester can fire this account's own notification triggers, which would
    email the customer before anyone has read the draft - the opposite of
    review-before-send. Set PROACTIVE_SET_REQUESTER=true only after checking
    the triggers in Admin Center.
    """
    note = (
        f"AUTOMATED DELAY CHECK - nothing has been sent to the customer.\n\n"
        f"Order: {item.order_name}\n"
        f"Customer: {item.customer_name or 'unknown'}"
        + (f" <{item.email}>" if item.email else "")
        + f"\nWhy flagged: {item.detail}\n"
        + (f"Tracking: {item.tracking}\n" if item.tracking else "")
        + f"\n--- SUGGESTED MESSAGE ---\n{draft.draft}\n"
    )
    if draft.agent_notes:
        note += "\n--- CHECK BEFORE SENDING ---\n" + "\n".join(
            f"- {n}" for n in draft.agent_notes
        )
    if draft.hand_to_agent:
        note += f"\n\nHANDED OVER: {draft.handover_reason}"

    ticket: dict[str, Any] = {
        "subject": f"Delay check: order {item.order_name} - {item.detail}",
        "comment": {"body": note, "public": False},
        "tags": ["proactive_delay_check", item.reason],
        "status": "new",
    }
    if _set_requester() and item.email:
        ticket["requester"] = {
            "name": item.customer_name or item.email,
            "email": item.email,
        }

    try:
        with ZendeskClient() as client:
            created = client.create_ticket(ticket)
        return created.get("id")
    except Exception as exc:
        log.warning("Could not raise a ticket for %s: %s", item.order_name, exc)
        return None


def send_digest(lines: list[str]) -> bool:
    """Post the morning summary to Slack, if a webhook is configured."""
    load_dotenv()
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        log.info("No SLACK_WEBHOOK_URL set - digest logged only")
        return False
    text = "*Orders needing a proactive update*\n" + "\n".join(lines)
    try:
        response = httpx.post(url, json={"text": text}, timeout=15.0)
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Could not post the digest to Slack: %s", exc)
        return False
