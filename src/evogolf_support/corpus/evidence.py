"""Pull the full thread for named tickets, so a decision can be checked in context.

The voice guide cites ticket numbers as evidence. To review a decision properly
you need to read what was actually said and when, so this assembles those
threads - dated, speaker-labelled, and already PII-redacted by the cleaner.
"""

from __future__ import annotations

from typing import Any

from .store import CorpusStore


def threads_for(store: CorpusStore, ticket_ids: list[int]) -> list[dict[str, Any]]:
    if not ticket_ids:
        return []
    agent_ids = store.agent_ids()
    placeholders = ",".join("?" * len(ticket_ids))
    tickets = store._conn.execute(  # noqa: SLF001
        f"SELECT t.id, t.subject, t.created_at, t.status, th.theme_key "
        f"FROM tickets t LEFT JOIN ticket_themes th ON th.ticket_id = t.id "
        f"WHERE t.id IN ({placeholders}) ORDER BY t.id",
        tuple(ticket_ids),
    ).fetchall()

    out = []
    for ticket in tickets:
        comments = store._conn.execute(  # noqa: SLF001
            "SELECT author_id, created_at, clean_body FROM comments "
            "WHERE ticket_id = ? AND TRIM(COALESCE(clean_body,'')) != '' "
            "ORDER BY created_at ASC",
            (ticket["id"],),
        ).fetchall()
        out.append({
            "id": ticket["id"],
            "subject": ticket["subject"],
            "created_at": ticket["created_at"],
            "status": ticket["status"],
            "theme": ticket["theme_key"],
            "messages": [
                {
                    "who": "AGENT" if c["author_id"] in agent_ids else "CUSTOMER",
                    "at": c["created_at"],
                    "text": c["clean_body"],
                }
                for c in comments
            ],
        })
    return out
