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
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

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


# The shop is in the UK, so "today" has to mean today in Bristol, not in UTC.
# In summer those differ by an hour, which is enough to put the first ticket
# of the morning on the wrong day.
LOCAL = ZoneInfo("Europe/London")

PRESETS = ("today", "yesterday", "week", "month", "last7", "last30")


def _utc(local_date: dt.date, end_of_day: bool = False) -> str:
    moment = dt.datetime.combine(
        local_date, dt.time.max if end_of_day else dt.time.min, tzinfo=LOCAL)
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Range:
    """A window to report on, resolved to UTC for querying."""

    since: str
    until: str
    label: str
    key: str


def resolve_range(preset: str = "last30", start: str = "", end: str = "") -> Range:
    """Turn a preset - or a custom pair of dates - into a window.

    Anything unrecognised falls back to the last 30 days rather than
    erroring: this is reached from a URL people edit and share.
    """
    today = dt.datetime.now(LOCAL).date()
    if preset == "custom" and start:
        try:
            first = dt.date.fromisoformat(start)
            last = dt.date.fromisoformat(end) if end else today
        except ValueError:
            return resolve_range("last30")
        if last < first:
            first, last = last, first
        label = (f"{first.strftime('%-d %b')} to {last.strftime('%-d %b %Y')}"
                 if first != last else first.strftime("%-d %b %Y"))
        return Range(_utc(first), _utc(last, True), label, "custom")

    if preset == "today":
        return Range(_utc(today), _utc(today, True), "Today", "today")
    if preset == "yesterday":
        day = today - dt.timedelta(days=1)
        return Range(_utc(day), _utc(day, True), "Yesterday", "yesterday")
    if preset == "week":
        first = today - dt.timedelta(days=today.weekday())
        return Range(_utc(first), _utc(today, True), "Week to date", "week")
    if preset == "month":
        first = today.replace(day=1)
        return Range(_utc(first), _utc(today, True), "Month to date", "month")
    if preset == "last7":
        return Range(_utc(today - dt.timedelta(days=6)), _utc(today, True),
                     "Last 7 days", "last7")
    return Range(_utc(today - dt.timedelta(days=29)), _utc(today, True),
                 "Last 30 days", "last30")


def _working_days(window: Range) -> int:
    first = dt.date.fromisoformat(window.since[:10])
    last = dt.date.fromisoformat(window.until[:10])
    days = 0
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5:
            days += 1
        cursor += dt.timedelta(days=1)
    return max(1, days)


def draft_adoption(store: CorpusStore, window: Range) -> dict[str, Any]:
    """For each draft, how much of it survived into the agent's reply."""
    agents = store.agent_ids()
    buckets = {"used_as_is": 0, "edited": 0, "low_overlap": 0,
               "no_reply_yet": 0, "handover": 0}
    scores: list[float] = []

    for record in store.drafts_between(window.since, window.until):
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


def usage(store: CorpusStore, window: Range) -> dict[str, Any]:
    """Is Zendesk actually being worked in?"""
    agents = store.agent_ids()
    placeholders = ",".join("?" for _ in agents) or "NULL"
    span = [window.since, window.until]

    tickets = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) c FROM tickets WHERE created_at BETWEEN ? AND ?", span
    ).fetchone()["c"]
    replies = store._conn.execute(  # noqa: SLF001
        f"SELECT COUNT(*) c FROM comments WHERE created_at BETWEEN ? AND ? "
        f"AND public = 1 AND author_id IN ({placeholders})", [*span, *agents]
    ).fetchone()["c"] if agents else 0
    answered = store._conn.execute(  # noqa: SLF001
        f"SELECT COUNT(DISTINCT t.id) c FROM tickets t JOIN comments c "
        f"ON c.ticket_id = t.id WHERE t.created_at BETWEEN ? AND ? "
        f"AND c.public = 1 AND c.author_id IN ({placeholders})", [*span, *agents]
    ).fetchone()["c"] if agents else 0

    return {
        "label": window.label,
        "tickets": tickets,
        "agent_replies": replies,
        "tickets_answered": answered,
        "tickets_per_working_day": round(tickets / _working_days(window), 1),
        "unanswered": max(0, tickets - answered),
        # Of the tickets that arrived, how many were actually answered from
        # Zendesk. A ticket answered from Gmail instead counts as unanswered
        # here, which is the point: it is the number that says whether
        # Zendesk is really the central place or just a copy of the inbox.
        "answered_percent": round(100 * answered / tickets) if tickets else None,
    }


def daily_counts(store: CorpusStore, window: Range) -> list[dict[str, Any]]:
    """Tickets and drafts per day, for a shape rather than a single number."""
    since, until = window.since, window.until
    tickets = {r["d"]: r["c"] for r in store._conn.execute(  # noqa: SLF001
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM tickets "
        "WHERE created_at BETWEEN ? AND ? GROUP BY d", (since, until))}
    drafts = {r["d"]: r["c"] for r in store._conn.execute(  # noqa: SLF001
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM drafts "
        "WHERE created_at BETWEEN ? AND ? GROUP BY d", (since, until))}

    first = dt.date.fromisoformat(since[:10])
    last = dt.date.fromisoformat(until[:10])
    # A long window would put hundreds of bars in a 900px chart, so beyond
    # six weeks the shape is shown for the most recent six.
    first = max(first, last - dt.timedelta(days=41))
    out, cursor = [], first
    while cursor <= last:
        day = cursor.isoformat()
        out.append({"date": day,
                    "tickets": tickets.get(day, 0),
                    "drafts": drafts.get(day, 0)})
        cursor += dt.timedelta(days=1)
    return out


def channel_mix(store: CorpusStore, window: Range) -> dict[str, int]:
    """Where conversations are happening. Gmail imports are tagged, so a
    channel that never moves into Zendesk shows up here rather than being
    assumed to have moved."""
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT COALESCE(via_channel,'unknown') ch, COUNT(*) c FROM tickets "
        "WHERE created_at BETWEEN ? AND ? GROUP BY ch ORDER BY c DESC",
        (window.since, window.until)
    ).fetchall()
    return {r["ch"]: r["c"] for r in rows}


def report(store: CorpusStore, window: Range) -> dict[str, Any]:
    return {
        "generated_at": dt.datetime.now(LOCAL).strftime("%-d %b %Y, %H:%M"),
        "window": window,
        "usage": usage(store, window),
        "adoption": draft_adoption(store, window),
        "daily": daily_counts(store, window),
        "channels": channel_mix(store, window),
    }
