"""Discover what Evolution Golf's tickets are actually about.

The first attempt at this used hand-written keyword patterns, and the data
rejected them: 78% of tickets matched none of the seven categories assumed,
and the Zendesk tags turned out to hold agent names rather than categories.
So the taxonomy is read from the tickets instead of imposed on them - Claude
proposes the themes from the subject lines, then assigns every ticket to one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from ..corpus.clean import redact
from ..corpus.store import CorpusStore
from .llm import MODEL, client

log = logging.getLogger(__name__)

# Subjects are short; this many per classification call keeps each request
# well inside a comfortable size while holding the call count down.
CLASSIFY_BATCH = 100


class Theme(BaseModel):
    key: str = Field(description="snake_case identifier, e.g. trolley_servicing")
    label: str = Field(description="Short human-readable name")
    definition: str = Field(description="One sentence: what belongs in this theme")


class Taxonomy(BaseModel):
    themes: list[Theme]
    notes: str = Field(
        description="Anything notable about the mix of tickets, including any "
        "that look like test, spam or internal tickets rather than customer support."
    )


class Assignment(BaseModel):
    ticket_id: int
    theme_key: str


class Assignments(BaseModel):
    assignments: list[Assignment]


DISCOVERY_PROMPT = """\
You are analysing the support inbox of Evolution Golf, a UK golf retailer \
(online and in-store: clubs, trolleys, bags, shoes, GPS units, custom fitting).

Below are the subject lines of {count} support tickets from the last 12 months, \
with any Zendesk tags. Propose a taxonomy of themes that describes what \
customers actually contact them about.

Rules:
- Derive the themes from what is actually here. Do not impose a generic \
e-commerce taxonomy.
- Aim for 8-14 themes. Each should be common enough to be worth writing a \
policy for.
- Include a theme for tickets that are not genuine customer support (test \
tickets, marketing, internal, spam) so they can be excluded later.
- Themes must be mutually exclusive enough that a ticket sits naturally in one.

Tickets:
{tickets}
"""

CLASSIFY_PROMPT = """\
Assign each ticket below to exactly one theme key from this taxonomy:

{taxonomy}

Return one assignment per ticket. Use the closest theme; if nothing fits, use \
the non-customer/other theme.

Tickets:
{tickets}
"""


def _ticket_lines(rows: list[Any]) -> str:
    lines = []
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        # Subjects carry customer names; redact before they leave the corpus.
        subject = redact(row["subject"] or "(no subject)")
        tag_text = f" [tags: {', '.join(tags)}]" if tags else ""
        lines.append(f"{row['id']}: {subject}{tag_text}")
    return "\n".join(lines)


def discover_themes(store: CorpusStore) -> Taxonomy:
    """Ask Claude to propose a taxonomy from the ticket subjects."""
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT id, subject, tags FROM tickets WHERE status != 'deleted' ORDER BY id"
    ).fetchall()
    if not rows:
        raise RuntimeError("No tickets in the corpus - run an export first.")

    prompt = DISCOVERY_PROMPT.format(count=len(rows), tickets=_ticket_lines(rows))
    log.info("Asking for a taxonomy over %s ticket subjects", len(rows))

    response = client().messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Taxonomy,
    )
    taxonomy = response.parsed_output
    log.info("Proposed %s themes", len(taxonomy.themes))
    return taxonomy


def classify_tickets(store: CorpusStore, taxonomy: Taxonomy) -> dict[int, str]:
    """Assign every ticket to one of the discovered themes."""
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT id, subject, tags FROM tickets WHERE status != 'deleted' ORDER BY id"
    ).fetchall()

    taxonomy_text = "\n".join(
        f"- {t.key}: {t.label} - {t.definition}" for t in taxonomy.themes
    )
    valid_keys = {t.key for t in taxonomy.themes}
    assignments: dict[int, str] = {}
    api = client()

    for start in range(0, len(rows), CLASSIFY_BATCH):
        batch = rows[start : start + CLASSIFY_BATCH]
        response = api.messages.parse(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},  # assignment, not analysis
            messages=[{
                "role": "user",
                "content": CLASSIFY_PROMPT.format(
                    taxonomy=taxonomy_text, tickets=_ticket_lines(batch)
                ),
            }],
            output_format=Assignments,
        )
        for item in response.parsed_output.assignments:
            if item.theme_key in valid_keys:
                assignments[item.ticket_id] = item.theme_key
        log.info(
            "Classified %s/%s tickets", len(assignments), len(rows)
        )

    return assignments
