"""Find the past tickets most like a new one.

SQLite's FTS5 is built in, so the corpus needs no extra service and no
embedding step. At ~400 usable tickets, BM25 over the subject and the cleaned
thread text retrieves well, and - unlike an embedding - the match is
inspectable: an agent can see which words drove it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..corpus.store import CorpusStore
from ..mining.run import NON_SUPPORT_THEMES

log = logging.getLogger(__name__)

INDEX_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS ticket_fts USING fts5(
    ticket_id UNINDEXED,
    subject,
    body,
    tokenize = 'porter unicode61'
);
"""

_WORD = re.compile(r"[A-Za-z0-9']+")
# Words too common in this corpus to discriminate between tickets.
_NOISE = {
    "the", "and", "for", "you", "your", "with", "this", "that", "have", "has",
    "from", "are", "was", "will", "would", "can", "not", "but", "any", "all",
    "hi", "hello", "thanks", "thank", "please", "regards", "order", "golf",
    "evolution", "email", "customer", "team",
}


def rebuild_index(store: CorpusStore) -> int:
    """(Re)build the search index over tickets worth learning from."""
    conn = store._conn  # noqa: SLF001
    conn.executescript(INDEX_SQL)
    conn.execute("DELETE FROM ticket_fts")

    agent_ids = store.agent_ids()
    if not agent_ids:
        return 0
    agent_ph = ",".join("?" * len(agent_ids))
    theme_ph = ",".join("?" * len(NON_SUPPORT_THEMES))

    rows = conn.execute(
        f"""
        SELECT t.id, t.subject, GROUP_CONCAT(c.clean_body, ' ') AS body
        FROM tickets t
        JOIN comments c ON c.ticket_id = t.id
        LEFT JOIN ticket_themes th ON th.ticket_id = t.id
        WHERE t.status IN ('solved','closed')
          AND TRIM(COALESCE(c.clean_body,'')) != ''
          AND (th.theme_key IS NULL OR th.theme_key NOT IN ({theme_ph}))
          AND EXISTS (
            SELECT 1 FROM comments a WHERE a.ticket_id = t.id
            AND a.author_id IN ({agent_ph})
            AND TRIM(COALESCE(a.clean_body,'')) != ''
          )
        GROUP BY t.id
        """,
        (*sorted(NON_SUPPORT_THEMES), *sorted(agent_ids)),
    ).fetchall()

    conn.executemany(
        "INSERT INTO ticket_fts (ticket_id, subject, body) VALUES (?,?,?)",
        [(r["id"], r["subject"] or "", r["body"] or "") for r in rows],
    )
    conn.commit()
    log.info("Search index rebuilt over %s tickets", len(rows))
    return len(rows)


def _query_terms(text: str, limit: int = 40) -> str:
    """Turn free text into a safe FTS5 OR query.

    User text goes nowhere near the query syntax: only word characters
    survive, so quotes, hyphens and FTS operators in a customer's message
    cannot change how the query is parsed.
    """
    seen: list[str] = []
    for word in _WORD.findall(text.lower()):
        if len(word) < 3 or word in _NOISE or word.isdigit():
            continue
        cleaned = word.replace("'", "")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
        if len(seen) >= limit:
            break
    return " OR ".join(seen)


def similar(
    store: CorpusStore,
    text: str,
    *,
    theme: str | None = None,
    limit: int = 6,
    exclude_ticket_id: int | None = None,
) -> list[dict[str, Any]]:
    """Past tickets most like this text, best first.

    ``exclude_ticket_id`` keeps a ticket out of its own results. Without it an
    evaluation retrieves the very reply it is supposed to be predicting, and
    the drafts look far better than they are.
    """
    query = _query_terms(text)
    if not query:
        return []

    conn = store._conn  # noqa: SLF001
    try:
        # Named parameters, not positional: this query interpolates an
        # optional JOIN whose placeholder sits BEFORE the MATCH in the SQL
        # text, and positional binding silently pairs them the wrong way
        # round - matching nothing and looking like "no similar tickets".
        params: dict[str, Any] = {"query": query, "limit": limit}
        exclude_clause = ""
        if exclude_ticket_id is not None:
            exclude_clause = "AND f.ticket_id != :exclude"
            params["exclude"] = exclude_ticket_id
        theme_join = ""
        if theme:
            theme_join = (
                "JOIN ticket_themes th ON th.ticket_id = f.ticket_id "
                "AND th.theme_key = :theme"
            )
            params["theme"] = theme
        rows = conn.execute(
            f"""
            SELECT f.ticket_id, bm25(ticket_fts) AS score
            FROM ticket_fts f {theme_join}
            WHERE ticket_fts MATCH :query {exclude_clause}
            ORDER BY score
            LIMIT :limit
            """,
            params,
        ).fetchall()
    except Exception as exc:
        log.warning("Search failed (%s); has the index been built?", exc)
        return []

    agent_ids = store.agent_ids()
    out = []
    for row in rows:
        ticket = conn.execute(
            "SELECT id, subject, created_at FROM tickets WHERE id = ?",
            (row["ticket_id"],),
        ).fetchone()
        comments = conn.execute(
            "SELECT author_id, created_at, clean_body FROM comments "
            "WHERE ticket_id = ? AND TRIM(COALESCE(clean_body,'')) != '' "
            "ORDER BY created_at ASC",
            (row["ticket_id"],),
        ).fetchall()
        out.append({
            "id": ticket["id"],
            "subject": ticket["subject"],
            "created_at": ticket["created_at"],
            "score": round(row["score"], 3),
            "messages": [
                {"who": "AGENT" if c["author_id"] in agent_ids else "CUSTOMER",
                 "at": c["created_at"], "text": c["clean_body"]}
                for c in comments
            ],
        })
    return out
