"""Post to Slack, where a person will actually see it.

Two things go here: the drafted reply as each ticket arrives, and the
morning digest from the delay sweep. Both exist so that reviewing this
system's work does not require anyone to go looking for it.

Silence is the point of the first one. A ticket that produces no Slack
message means something went wrong - which is a signal, where an empty
Zendesk sidebar is just an empty sidebar.
"""

from __future__ import annotations

import logging
import os

import httpx

from .config import load_dotenv
from .drafting.generate import Draft

log = logging.getLogger(__name__)

# Slack rejects a block over 3000 characters. Drafts are short by design,
# but a customer's opening message is not always.
BLOCK_LIMIT = 2900


def configured() -> bool:
    load_dotenv()
    return bool(os.environ.get("SLACK_WEBHOOK_URL", "").strip())


def post(text: str, blocks: list[dict] | None = None) -> bool:
    """Send one message. Never raises: Slack being down is not a reason to
    fail a draft that has already been written onto the ticket."""
    load_dotenv()
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        log.info("No SLACK_WEBHOOK_URL set - nothing posted to Slack")
        return False
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        response = httpx.post(url, json=payload, timeout=15.0)
        response.raise_for_status()
        return True
    except Exception as exc:                            # noqa: BLE001
        log.warning("Could not post to Slack: %s", exc)
        return False


def _trim(text: str, limit: int = BLOCK_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def ticket_url(ticket_id: int) -> str:
    load_dotenv()
    subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "").strip()
    return (f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"
            if subdomain else "")


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _trim(text)}}


def post_draft(ticket_id: int, subject: str, asked: str, draft: Draft) -> bool:
    """Put a drafted reply in front of a person as the ticket arrives."""
    link = ticket_url(ticket_id)
    heading = f"*<{link}|#{ticket_id}>* {subject}" if link else f"*#{ticket_id}* {subject}"

    if draft.hand_to_agent:
        reason = draft.handover_reason or "The settled policy says a person should handle this."
        return post(
            f"Needs an agent: #{ticket_id} {subject}",
            [
                _section(f":warning: {heading}\n*Needs an agent - no draft written*"),
                _section(f"_Customer asked:_\n>{_trim(asked, 600)}"),
                _section(reason),
            ],
        )

    meta = [f"confidence: {draft.confidence or '?'}"]
    if draft.rules_applied:
        meta.append("rules " + ", ".join(str(r) for r in draft.rules_applied))
    blocks = [
        _section(f"{heading}\n_{' | '.join(meta)}_"),
        _section(f"_Customer asked:_\n>{_trim(asked, 600)}"),
        _section(f"*Suggested reply*\n```{_trim(draft.draft, 2000)}```"),
    ]
    if draft.agent_notes:
        blocks.append(_section(
            "*Check before sending*\n"
            + "\n".join(f"• {n}" for n in draft.agent_notes)
        ))
    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn",
        "text": "Draft only - nothing has reached the customer. "
                "It is also on the ticket as an internal note.",
    }]})
    return post(f"Draft ready for #{ticket_id}: {subject}", blocks)
