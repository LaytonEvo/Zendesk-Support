"""Distil each theme's agent replies into reviewable practice and voice.

The output is deliberately shaped for a human to correct, not to approve:
alongside the rules it extracts, it is asked to surface the places where
agents handled similar tickets differently. Those disagreements are the
decisions only the business can make, and they are the point of the review.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..corpus.store import CorpusStore
from .llm import MODEL, client

log = logging.getLogger(__name__)

# Enough threads to see the pattern without paying for the whole theme.
MAX_TICKETS_PER_THEME = 30
MAX_THREAD_CHARS = 1800


class Rule(BaseModel):
    rule: str = Field(description="The practice, stated as an instruction")
    confidence: str = Field(description="high, medium or low, given the evidence seen")
    basis: str = Field(description="What in the replies supports this")


class Disagreement(BaseModel):
    question: str = Field(description="The decision the business needs to make")
    variant_a: str = Field(description="One way it was handled")
    variant_b: str = Field(description="The other way it was handled")


class ThemeGuide(BaseModel):
    summary: str = Field(description="Two sentences on what this theme involves")
    voice_notes: list[str] = Field(description="How the team writes: tone, formality, habits")
    rules: list[Rule]
    disagreements: list[Disagreement] = Field(
        description="Places where similar tickets were handled differently. "
        "Report these rather than picking a winner."
    )
    stock_phrases: list[str] = Field(
        description="Short phrasings the team reuses, quoted as written"
    )
    escalation: list[str] = Field(
        description="When this stops being a reply and becomes an escalation"
    )


PROMPT = """\
You are documenting how the support team at Evolution Golf, a UK golf retailer, \
actually handles one category of customer enquiry, so that an AI assistant can \
draft replies in their voice and within their policy.

Theme: {label}
Definition: {definition}

Below are {count} real ticket threads from this theme. Customer messages are \
marked CUSTOMER and the team's replies AGENT. Personal details have been \
redacted; order numbers and product names are intact.

Extract:
- how the team writes (tone, formality, greeting and closing habits, how much \
detail they give, whether they apologise, how they handle bad news)
- the practice they follow, as rules an assistant could apply
- **the places where similar tickets were handled differently** - do not resolve \
these, report them as decisions for the business
- phrasings they reuse
- when the matter stops being a reply and becomes an escalation

Be concrete and specific to what you see. Where the evidence is thin, say so \
by marking the rule's confidence low rather than inventing a policy.

Threads:
{threads}
"""


def _threads(store: CorpusStore, theme_key: str) -> list[str]:
    agent_ids = store.agent_ids()
    rows = store._conn.execute(  # noqa: SLF001
        """
        SELECT t.id FROM tickets t
        JOIN ticket_themes th ON th.ticket_id = t.id
        WHERE th.theme_key = ? AND t.status != 'deleted'
        ORDER BY t.created_at DESC
        """,
        (theme_key,),
    ).fetchall()

    threads: list[str] = []
    for row in rows:
        comments = store._conn.execute(  # noqa: SLF001
            "SELECT author_id, clean_body FROM comments "
            "WHERE ticket_id = ? AND TRIM(COALESCE(clean_body,'')) != '' "
            "ORDER BY created_at ASC",
            (row["id"],),
        ).fetchall()
        if not any(c["author_id"] in agent_ids for c in comments):
            continue  # no agent reply: nothing to learn about the team's voice

        parts = []
        for comment in comments:
            who = "AGENT" if comment["author_id"] in agent_ids else "CUSTOMER"
            parts.append(f"{who}: {comment['clean_body']}")
        thread = "\n".join(parts)[:MAX_THREAD_CHARS]
        threads.append(f"--- ticket {row['id']} ---\n{thread}")
        if len(threads) >= MAX_TICKETS_PER_THEME:
            break
    return threads


def mine_theme(store: CorpusStore, theme_key: str, label: str, definition: str) -> ThemeGuide | None:
    threads = _threads(store, theme_key)
    if not threads:
        log.warning("No usable threads for theme %s", theme_key)
        return None

    log.info("Mining %s from %s threads", theme_key, len(threads))
    response = client().messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{
            "role": "user",
            "content": PROMPT.format(
                label=label,
                definition=definition,
                count=len(threads),
                threads="\n\n".join(threads),
            ),
        }],
        output_format=ThemeGuide,
    )
    return response.parsed_output
