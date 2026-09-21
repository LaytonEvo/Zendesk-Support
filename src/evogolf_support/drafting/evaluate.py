"""Draft replies for tickets that were already answered, and log both.

The only honest test of this system is whether its draft would have been a
reasonable reply to a real ticket. So we take solved tickets, hide everything
the team wrote, draft from the customer's opening message alone, and log the
draft beside what the team actually sent.

The ticket under test is excluded from retrieval, or the system would be
grading itself against an example containing the answer.
"""

from __future__ import annotations

import json
import logging

from ..corpus.store import CorpusStore
from ..mining.run import NON_SUPPORT_THEMES
from . import shopify
from .generate import draft_reply

log = logging.getLogger(__name__)


def sample_tickets(store: CorpusStore, limit: int, theme: str | None = None) -> list[int]:
    """Recent solved tickets that have both a customer question and a reply."""
    agent_ids = store.agent_ids()
    if not agent_ids:
        return []
    params: dict[str, object] = {"limit": limit}
    for i, aid in enumerate(sorted(agent_ids)):
        params[f"a{i}"] = aid
    agent_ph = ",".join(f":a{i}" for i in range(len(agent_ids)))
    for i, t in enumerate(sorted(NON_SUPPORT_THEMES)):
        params[f"n{i}"] = t
    noise_ph = ",".join(f":n{i}" for i in range(len(NON_SUPPORT_THEMES)))

    theme_clause = ""
    if theme:
        theme_clause = "AND th.theme_key = :theme"
        params["theme"] = theme

    rows = store._conn.execute(  # noqa: SLF001
        f"""
        SELECT t.id FROM tickets t
        LEFT JOIN ticket_themes th ON th.ticket_id = t.id
        WHERE t.status IN ('solved','closed')
          AND (th.theme_key IS NULL OR th.theme_key NOT IN ({noise_ph}))
          {theme_clause}
          AND EXISTS (SELECT 1 FROM comments c WHERE c.ticket_id = t.id
                      AND c.author_id NOT IN ({agent_ph})
                      AND TRIM(COALESCE(c.clean_body,'')) != '')
          AND EXISTS (SELECT 1 FROM comments c WHERE c.ticket_id = t.id
                      AND c.author_id IN ({agent_ph})
                      AND TRIM(COALESCE(c.clean_body,'')) != '')
        ORDER BY t.created_at DESC
        LIMIT :limit
        """,
        params,
    ).fetchall()
    return [r["id"] for r in rows]


def evaluate_ticket(store: CorpusStore, ticket_id: int) -> dict:
    """Draft a reply to a real ticket from its opening message alone."""
    agent_ids = store.agent_ids()
    ticket = store._conn.execute(  # noqa: SLF001
        "SELECT id, subject FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    comments = store._conn.execute(  # noqa: SLF001
        "SELECT author_id, created_at, clean_body FROM comments "
        "WHERE ticket_id = ? AND TRIM(COALESCE(clean_body,'')) != '' "
        "ORDER BY created_at ASC",
        (ticket_id,),
    ).fetchall()

    opening = next((c for c in comments if c["author_id"] not in agent_ids), None)
    actual = next((c for c in comments if c["author_id"] in agent_ids), None)
    if opening is None or actual is None:
        return {}

    theme_row = store._conn.execute(  # noqa: SLF001
        "SELECT theme_key FROM ticket_themes WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()

    # The evaluation must exercise the same path an agent gets, order lookup
    # included - otherwise it measures a system nobody will actually use.
    subject = ticket["subject"] or ""
    who = store.requester(ticket_id) or {}
    order_context = shopify.context_for_ticket(
        f"{subject} {opening['clean_body']}",
        email=who.get("email") or None,
        name=who.get("name") or None,
    ) or None

    draft = draft_reply(
        store,
        subject=subject,
        body=opening["clean_body"],
        theme=theme_row["theme_key"] if theme_row else None,
        order_context=order_context,
        exclude_ticket_id=ticket_id,   # never retrieve the answer we are predicting
    )
    return {
        "ticket": ticket_id,
        "subject": ticket["subject"],
        "theme": theme_row["theme_key"] if theme_row else None,
        "customer_asked": opening["clean_body"],
        "team_actually_replied": actual["clean_body"],
        "order_context_found": bool(order_context),
        "draft": draft.model_dump(),
    }


def run_evaluation(store: CorpusStore, limit: int, theme: str | None = None) -> int:
    ids = sample_tickets(store, limit, theme)
    log.info("Evaluating drafts against %s real tickets", len(ids))
    done = 0
    with_orders = 0
    for ticket_id in ids:
        try:
            result = evaluate_ticket(store, ticket_id)
        except Exception as exc:
            log.warning("Draft failed for ticket %s: %s", ticket_id, exc)
            continue
        if result:
            log.info("eval/%s: %s", ticket_id, json.dumps(result))
            done += 1
            with_orders += 1 if result.get("order_context_found") else 0
    log.info(
        "eval/done: %s drafts, %s with live order data%s",
        done, with_orders,
        "" if shopify.configured() else " (Shopify is not configured)",
    )
    return done
