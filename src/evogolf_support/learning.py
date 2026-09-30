"""Learn from what the team actually sent.

A draft is a guess. The reply a person chose to send instead is the answer.
Where the two differ, that pair - the customer's message, our suggestion,
and the reply that went out - is the most direct evidence there is of how
this business wants these handled, and it is worth more than any number of
older examples that were never compared against an attempt.

So corrections are collected and fed back into the prompt. Nothing here
retrains anything: the drafts are built from retrieved examples, and this
adds a sharper class of example.
"""

from __future__ import annotations

import logging
from typing import Any

from .corpus.store import CorpusStore
from .metrics import USED_AS_IS, similarity

log = logging.getLogger(__name__)

# Below this, the agent changed enough that it is worth learning from. At or
# above it they sent our draft, and there is nothing to learn.
WORTH_LEARNING = USED_AS_IS
# Two lines of acknowledgement teach nothing and would crowd out real
# examples in the prompt.
MIN_SENT_CHARS = 120


def collect(store: CorpusStore, days: int = 30) -> int:
    """Find drafts the team rewrote, and keep the pair. Returns how many."""
    from .metrics import _utc, LOCAL
    import datetime as dt

    since = (dt.datetime.now(LOCAL) - dt.timedelta(days=days)).date()
    agents = store.agent_ids()
    if not agents:
        return 0
    already = store.corrected_comment_ids()
    found = 0

    for record in store.drafts_between(_utc(since), _utc(dt.datetime.now(LOCAL).date(), True)):
        if record["handover"] or record["comment_id"] in already:
            continue
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT author_id, clean_body FROM comments "
            "WHERE ticket_id = ? AND public = 1 AND created_at > ? "
            "ORDER BY created_at",
            (record["ticket_id"], record["created_at"]),
        ).fetchall()
        sent = next((r["clean_body"] for r in rows
                     if r["author_id"] in agents and (r["clean_body"] or "").strip()),
                    None)
        if not sent or len(sent.strip()) < MIN_SENT_CHARS:
            continue
        score = similarity(record["draft"], sent)
        if score >= WORTH_LEARNING:
            continue                        # they sent ours; nothing to learn

        question = store._conn.execute(  # noqa: SLF001
            "SELECT clean_body FROM comments WHERE id = ?", (record["comment_id"],)
        ).fetchone()
        theme = store._conn.execute(  # noqa: SLF001
            "SELECT theme_key FROM ticket_themes WHERE ticket_id = ?",
            (record["ticket_id"],)
        ).fetchone()
        store.record_correction(
            record["ticket_id"], record["comment_id"], record["created_at"],
            theme["theme_key"] if theme else None,
            (question["clean_body"] if question else "") or "",
            record["draft"], sent, score,
        )
        found += 1

    if found:
        log.info("Learned from %s reply(ies) the team rewrote", found)
    return found


def render_for_prompt(corrections: list[dict[str, Any]]) -> str:
    """Show the pairs to the drafting model, newest first."""
    if not corrections:
        return ""
    blocks = []
    for c in corrections:
        blocks.append(
            "Customer asked:\n" + (c["question"] or "").strip()[:600]
            + "\n\nWhat was suggested:\n" + (c["suggested"] or "").strip()[:900]
            + "\n\nWhat the team actually sent:\n" + (c["sent"] or "").strip()[:900]
        )
    return "\n\n---\n\n".join(blocks)
