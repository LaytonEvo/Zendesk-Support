"""Generate a reply draft for a ticket.

Three things go into the prompt, in order of authority:

1. the settled policy - business decisions that override everything else
2. the theme's mined voice and rules - how the team writes about this subject
3. the most similar past threads - concrete examples of the house voice

The draft is never sent. It carries the rules it applied and the tickets it
leaned on so an agent can check it in seconds, and it can decline to draft at
all where the policy says a human should take it.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from ..corpus.store import CorpusStore
from ..mining.llm import MODEL, client
from ..mining.run import GUIDE_KEY
from ..policy import as_prompt_text
from .retrieve import similar

log = logging.getLogger(__name__)

MAX_EXAMPLE_CHARS = 1500


class Draft(BaseModel):
    hand_to_agent: bool = Field(
        description="True when the settled policy says a human should take this, "
        "or when the evidence does not support a confident reply."
    )
    handover_reason: str = Field(description="Why, if hand_to_agent is true. Else empty.")
    draft: str = Field(description="The reply, ready for an agent to review and send.")
    confidence: str = Field(description="high, medium or low")
    rules_applied: list[int] = Field(description="Settled policy rule ids this reply relies on")
    tickets_referenced: list[int] = Field(description="Past ticket ids that informed it")
    agent_notes: list[str] = Field(
        description="Anything the agent must check or fill in before sending - "
        "figures to confirm, actions to take, facts not held in the corpus."
    )


PROMPT = """\
You are drafting a support reply for Evolution Golf, a UK golf retailer, in \
the voice of their support team. An agent will review and send it - never \
write as though it goes out unchecked.

{policy}

HOW THE TEAM WRITES ABOUT THIS KIND OF TICKET
{guide}

SIMILAR PAST TICKETS AND HOW THEY WERE ANSWERED
{examples}

THE TICKET TO ANSWER
Subject: {subject}

{body}

Write the reply. Rules:
- Follow the settled policy above even where a past example did otherwise, \
and say which rule ids you relied on.
- Match the team's voice, not a generic support register.
- Never invent an order status, a date, a stock position, a price or a refund \
figure. Where the reply needs one, leave a clear placeholder in square \
brackets and add an agent note.
- Where the policy says to hand over, set hand_to_agent and explain why \
instead of drafting around it.
"""


def _render_examples(examples: list[dict]) -> str:
    if not examples:
        return "(none found - say so in your confidence and agent notes)"
    out = []
    for ex in examples:
        thread = "\n".join(
            f"{m['who']}: {m['text']}" for m in ex["messages"]
        )[:MAX_EXAMPLE_CHARS]
        out.append(f"--- ticket {ex['id']} ({ex.get('created_at','')}) "
                   f"{ex.get('subject','')}\n{thread}")
    return "\n\n".join(out)


def _theme_guide(store: CorpusStore, theme: str | None) -> str:
    raw = store.get_state(GUIDE_KEY)
    if not raw:
        return "(no mined guide available yet)"
    try:
        guides = json.loads(raw)
    except json.JSONDecodeError:
        return "(guide could not be read)"
    guide = guides.get(theme) if theme else None
    if guide is None:
        # No theme, or a theme we never mined: give the voice notes from the
        # largest guide so the register is still right.
        guide = max(guides.values(), key=lambda g: len(g.get("rules", [])), default=None)
        if guide is None:
            return "(no mined guide available yet)"
    parts = [guide.get("summary", "")]
    parts += ["VOICE:"] + [f"- {n}" for n in guide.get("voice_notes", [])]
    parts += ["PRACTICE:"] + [
        f"- [{r['confidence']}] {r['rule']}" for r in guide.get("rules", [])
    ]
    parts += ["PHRASES THEY REUSE:"] + [f"- {p}" for p in guide.get("stock_phrases", [])[:12]]
    if guide.get("escalation"):
        parts += ["ESCALATE WHEN:"] + [f"- {e}" for e in guide["escalation"]]
    return "\n".join(parts)


def draft_reply(
    store: CorpusStore,
    *,
    subject: str,
    body: str,
    theme: str | None = None,
    order_context: str | None = None,
    exclude_ticket_id: int | None = None,
) -> Draft:
    examples = similar(
        store, f"{subject} {body}", theme=theme, limit=6,
        exclude_ticket_id=exclude_ticket_id,
    )

    # With no theme given, borrow the one from the closest match.
    if theme is None and examples:
        row = store._conn.execute(  # noqa: SLF001
            "SELECT theme_key FROM ticket_themes WHERE ticket_id = ?",
            (examples[0]["id"],),
        ).fetchone()
        theme = row["theme_key"] if row else None

    prompt = PROMPT.format(
        policy=as_prompt_text(),
        guide=_theme_guide(store, theme),
        examples=_render_examples(examples),
        subject=subject,
        body=body,
    )
    if order_context:
        prompt += f"\n\nLIVE ORDER DATA (authoritative - prefer it over anything above)\n{order_context}"

    response = client().messages.parse(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Draft,
    )
    result = response.parsed_output
    log.info(
        "Drafted for theme=%s: handover=%s confidence=%s rules=%s tickets=%s",
        theme, result.hand_to_agent, result.confidence,
        result.rules_applied, result.tickets_referenced,
    )
    return result
