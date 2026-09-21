"""Re-classify tickets whose subject line does not reveal their intent.

Many subjects are a bare order number, which pushed unrelated requests into
one catch-all theme. Those tickets are re-read using the first customer
message so amendment, returns and delivery chasing land where they belong.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from ..corpus.store import CorpusStore
from .discover import Assignments, Taxonomy, _ticket_lines  # noqa: F401
from .llm import MODEL, client

log = logging.getLogger(__name__)

# Themes that a subject line alone cannot separate.
AMBIGUOUS_THEMES = (
    "order_status_delivery",
    "order_amendment_cancellation",
    "returns_exchanges_refunds",
)

# Bodies are far longer than subjects, so batches are smaller.
BATCH = 25
# Enough of the opening message to show intent, without paying for whole threads.
BODY_CHARS = 700

PROMPT = """\
Each ticket below shows its subject and the opening customer message. Assign \
each to exactly one theme key from this taxonomy:

{taxonomy}

The subject lines are often just an order number, so judge by the message. \
Assign the theme that matches what the customer is actually asking for.

Tickets:
{tickets}
"""


class _Row(BaseModel):
    ticket_id: int
    text: str


def _ambiguous_rows(store: CorpusStore) -> list[_Row]:
    placeholders = ",".join("?" * len(AMBIGUOUS_THEMES))
    agent_ids = store.agent_ids()
    agent_clause = ""
    params: list[object] = list(AMBIGUOUS_THEMES)
    if agent_ids:
        agent_ph = ",".join("?" * len(agent_ids))
        agent_clause = f"AND c.author_id NOT IN ({agent_ph})"
        params.extend(sorted(agent_ids))

    # The earliest non-agent comment is the customer's opening message.
    sql = f"""
        SELECT t.id AS ticket_id, t.subject AS subject, (
            SELECT c.clean_body FROM comments c
            WHERE c.ticket_id = t.id
              AND TRIM(COALESCE(c.clean_body,'')) != ''
              {agent_clause}
            ORDER BY c.created_at ASC LIMIT 1
        ) AS opening
        FROM tickets t
        JOIN ticket_themes th ON th.ticket_id = t.id
        WHERE th.theme_key IN ({placeholders})
        ORDER BY t.id
    """
    rows = store._conn.execute(sql, tuple(params)).fetchall()  # noqa: SLF001

    out: list[_Row] = []
    for row in rows:
        opening = (row["opening"] or "").strip()
        if not opening:
            continue  # nothing to re-read; leave the subject-based assignment
        subject = (row["subject"] or "(no subject)").strip()
        out.append(
            _Row(
                ticket_id=row["ticket_id"],
                text=f"{row['ticket_id']}: [{subject}] {opening[:BODY_CHARS]}",
            )
        )
    return out


def reclassify(store: CorpusStore, taxonomy: Taxonomy) -> dict[int, str]:
    rows = _ambiguous_rows(store)
    if not rows:
        return {}

    taxonomy_text = "\n".join(
        f"- {t.key}: {t.label} - {t.definition}" for t in taxonomy.themes
    )
    valid = {t.key for t in taxonomy.themes}
    api = client()
    updated: dict[int, str] = {}

    log.info("Re-reading %s tickets whose subject was ambiguous", len(rows))
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        response = api.messages.parse(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            messages=[{
                "role": "user",
                "content": PROMPT.format(
                    taxonomy=taxonomy_text,
                    tickets="\n\n".join(r.text for r in batch),
                ),
            }],
            output_format=Assignments,
        )
        for item in response.parsed_output.assignments:
            if item.theme_key in valid:
                updated[item.ticket_id] = item.theme_key

    store.set_ticket_themes(updated)
    log.info("Re-classified %s tickets from message bodies", len(updated))
    return updated
