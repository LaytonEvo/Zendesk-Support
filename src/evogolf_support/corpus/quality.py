"""Measure the corpus without reading it.

Two questions need answering before the corpus is worth mining, and neither
should require a human to read customer messages:

1. **Coverage** - did the export actually get everything, or did it stop early?
2. **Cleaning** - is the stripping too aggressive (throwing away real replies)
   or too lax (leaving quoted history and PII behind)?

Both are answered with counts and averages. No message content is returned,
so the report is safe to log, expose and paste into a conversation.
"""

from __future__ import annotations

from typing import Any

from .store import CorpusStore

# Traces that should not survive cleaning. If these show up in clean_body,
# the corresponding stripper is missing cases.
LEFTOVER_PROBES = {
    "zendesk_footer": "%Open Ticket #%",
    "quoted_reply": "% wrote:%",
    "signoff_kind_regards": "%Kind regards%",
    "unredacted_email": "%@%.%",
}


def report(store: CorpusStore) -> dict[str, Any]:
    conn = store._conn  # noqa: SLF001 - reporting is part of the store's contract

    def one(sql: str, *args: Any) -> Any:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None

    tickets = one("SELECT COUNT(*) FROM tickets") or 0
    comments = one("SELECT COUNT(*) FROM comments") or 0

    coverage = {
        "tickets": tickets,
        "lowest_ticket_id": one("SELECT MIN(id) FROM tickets"),
        "highest_ticket_id": one("SELECT MAX(id) FROM tickets"),
        "earliest_created": one("SELECT MIN(created_at) FROM tickets"),
        "latest_created": one("SELECT MAX(created_at) FROM tickets"),
        # A large gap between the count and the id range means most ids were
        # never real tickets (spam caught as suspended, or deleted) - worth
        # knowing before concluding the export missed something.
        "id_range_span": (
            (one("SELECT MAX(id) FROM tickets") or 0)
            - (one("SELECT MIN(id) FROM tickets") or 0)
            + 1
            if tickets
            else 0
        ),
        "tickets_with_no_comments": one(
            "SELECT COUNT(*) FROM tickets t "
            "WHERE NOT EXISTS (SELECT 1 FROM comments c WHERE c.ticket_id = t.id)"
        ),
    }

    by_status = {
        row["status"] or "unknown": row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status ORDER BY n DESC"
        )
    }

    agent_ids = store.agent_ids()
    agent_comments = 0
    if agent_ids:
        placeholders = ",".join("?" * len(agent_ids))
        agent_comments = one(
            f"SELECT COUNT(*) FROM comments WHERE author_id IN ({placeholders})",
            *sorted(agent_ids),
        )

    cleaning = {
        "comments": comments,
        "public_comments": one("SELECT COUNT(*) FROM comments WHERE public = 1"),
        "agent_comments": agent_comments,
        "empty_after_cleaning": one(
            "SELECT COUNT(*) FROM comments "
            "WHERE clean_body IS NULL OR TRIM(clean_body) = ''"
        ),
        "avg_raw_length": _round(one("SELECT AVG(LENGTH(body)) FROM comments")),
        "avg_clean_length": _round(one("SELECT AVG(LENGTH(clean_body)) FROM comments")),
    }
    if cleaning["avg_raw_length"]:
        cleaning["percent_stripped"] = _round(
            100 * (1 - (cleaning["avg_clean_length"] or 0) / cleaning["avg_raw_length"])
        )

    leftovers = {
        name: one(
            "SELECT COUNT(*) FROM comments WHERE clean_body LIKE ?", pattern
        )
        for name, pattern in LEFTOVER_PROBES.items()
    }

    return {
        "coverage": coverage,
        "tickets_by_status": by_status,
        "cleaning": cleaning,
        "leftovers_in_cleaned_text": leftovers,
    }


def _round(value: Any) -> Any:
    return round(value, 1) if isinstance(value, (int, float)) else value
