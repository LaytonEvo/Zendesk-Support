"""Is Zendesk being used, and are the drafts being relied on?

Two questions, and the second is the one that needs evidence rather than
opinion. Every draft is kept, so it can be compared against the reply the
agent actually sent afterwards. A reply that closely
matches the draft was used; one that shares little with it either was not,
or was rewritten so heavily that nothing of the draft survives.

That last distinction cannot be made and is not claimed. A reply keeping the
draft's facts but rewriting every sentence scores about the same as an
unrelated reply that happens to share the sign-off - measured, both land
near 0.3. So the third bucket is reported as "little or no overlap" rather
than "ignored", and the number is read as a floor on adoption, not a verdict
on the team.

Counts only. No customer names or addresses leave this module.
"""

from __future__ import annotations

import datetime as dt
import difflib
import logging
import re
from typing import Any

from .corpus.store import CorpusStore

log = logging.getLogger(__name__)

# How close a sent reply has to be to the draft to count as used. Measured
# against real drafts: a changed greeting scores ~0.95, a full reword ~0.3,
# an unrelated reply sharing only the sign-off ~0.29. The first is reliably
# detectable; the last two are not distinguishable from each other, which is
# why the bottom bucket claims nothing about intent.
USED_AS_IS = 0.80
EDITED = 0.45

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def similarity(draft: str, sent: str) -> float:
    a, b = _normalise(draft), _normalise(sent)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _iso(days_ago: int, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def draft_adoption(store: CorpusStore, days: int = 30) -> dict[str, Any]:
    """For each draft, how much of it survived into the agent's reply."""
    since = _iso(days)
    agents = store.agent_ids()
    buckets = {"used_as_is": 0, "edited": 0, "low_overlap": 0,
               "no_reply_yet": 0, "handover": 0}
    scores: list[float] = []

    for record in store.drafts_since(since):
        if record["handover"]:
            # No draft was written, so there is nothing to adopt.
            buckets["handover"] += 1
            continue
        replies = store._conn.execute(  # noqa: SLF001
            "SELECT author_id, clean_body FROM comments "
            "WHERE ticket_id = ? AND public = 1 AND created_at > ? "
            "ORDER BY created_at",
            (record["ticket_id"], record["created_at"]),
        ).fetchall()
        sent = next((r["clean_body"] for r in replies
                     if r["author_id"] in agents and (r["clean_body"] or "").strip()),
                    None)
        if not sent:
            buckets["no_reply_yet"] += 1
            continue
        score = similarity(record["draft"], sent)
        scores.append(score)
        if score >= USED_AS_IS:
            buckets["used_as_is"] += 1
        elif score >= EDITED:
            buckets["edited"] += 1
        else:
            buckets["low_overlap"] += 1

    judged = buckets["used_as_is"] + buckets["edited"] + buckets["low_overlap"]
    return {
        **buckets,
        "drafts": sum(buckets.values()),
        "judged": judged,
        "adoption_percent": round(
            100 * (buckets["used_as_is"] + buckets["edited"]) / judged) if judged else None,
        "average_similarity": round(sum(scores) / len(scores), 2) if scores else None,
    }


def usage(store: CorpusStore, days: int = 30) -> dict[str, Any]:
    """Is Zendesk actually being worked in?"""
    since = _iso(days)
    agents = store.agent_ids()
    placeholders = ",".join("?" for _ in agents) or "NULL"
    params = [since, *agents]

    tickets = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) c FROM tickets WHERE created_at >= ?", (since,)
    ).fetchone()["c"]
    replies = store._conn.execute(  # noqa: SLF001
        f"SELECT COUNT(*) c FROM comments WHERE created_at >= ? AND public = 1 "
        f"AND author_id IN ({placeholders})", params
    ).fetchone()["c"] if agents else 0
    answered = store._conn.execute(  # noqa: SLF001
        f"SELECT COUNT(DISTINCT ticket_id) c FROM comments WHERE created_at >= ? "
        f"AND public = 1 AND author_id IN ({placeholders})", params
    ).fetchone()["c"] if agents else 0

    working_days = max(1, round(days * 5 / 7))
    return {
        "days": days,
        "tickets": tickets,
        "agent_replies": replies,
        "tickets_answered": answered,
        "tickets_per_working_day": round(tickets / working_days, 1),
        "unanswered": max(0, tickets - answered),
    }


def daily_counts(store: CorpusStore, days: int = 14) -> list[dict[str, Any]]:
    """Tickets and drafts per day, for a shape rather than a single number."""
    since = _iso(days)
    tickets = {r["d"]: r["c"] for r in store._conn.execute(  # noqa: SLF001
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM tickets "
        "WHERE created_at >= ? GROUP BY d", (since,))}
    drafts = {r["d"]: r["c"] for r in store._conn.execute(  # noqa: SLF001
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM drafts "
        "WHERE created_at >= ? GROUP BY d", (since,))}

    today = dt.datetime.now(dt.timezone.utc).date()
    out = []
    for offset in range(days - 1, -1, -1):
        day = (today - dt.timedelta(days=offset)).isoformat()
        out.append({"date": day,
                    "tickets": tickets.get(day, 0),
                    "drafts": drafts.get(day, 0)})
    return out


def channel_mix(store: CorpusStore, days: int = 30) -> dict[str, int]:
    """Where conversations are happening. Gmail imports are tagged, so a
    channel that never moves into Zendesk shows up here rather than being
    assumed to have moved."""
    since = _iso(days)
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT COALESCE(via_channel,'unknown') ch, COUNT(*) c FROM tickets "
        "WHERE created_at >= ? GROUP BY ch ORDER BY c DESC", (since,)
    ).fetchall()
    return {r["ch"]: r["c"] for r in rows}


def report(store: CorpusStore, days: int = 30) -> dict[str, Any]:
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "usage": usage(store, days),
        "adoption": draft_adoption(store, days),
        "daily": daily_counts(store, 14),
        "channels": channel_mix(store, days),
    }
