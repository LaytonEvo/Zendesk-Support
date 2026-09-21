"""SQLite store for the exported ticket corpus.

SQLite keeps the corpus a single portable file with no service to run, which
suits a ~1,300-ticket dataset. The file holds customer data, so it is
gitignored and should never be committed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    subject         TEXT,
    status          TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    requester_id    INTEGER,
    assignee_id     INTEGER,
    group_id        INTEGER,
    via_channel     TEXT,
    tags            TEXT,
    satisfaction    TEXT,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL,
    author_id   INTEGER,
    public      INTEGER,
    created_at  TEXT,
    body        TEXT,
    clean_body  TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(author_id);

CREATE TABLE IF NOT EXISTS users (
    id      INTEGER PRIMARY KEY,
    name    TEXT,
    email   TEXT,
    role    TEXT
);

CREATE TABLE IF NOT EXISTS ticket_themes (
    ticket_id   INTEGER PRIMARY KEY,
    theme_key   TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_ticket_themes_key ON ticket_themes(theme_key);

CREATE TABLE IF NOT EXISTS export_state (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


class CorpusStore:
    def __init__(self, path: Path, *, timeout: float = 30.0):
        self.path = path
        # The export runs in a background thread while HTTP requests read
        # stats, so two connections touch this file at once. WAL lets readers
        # proceed during a write, and the timeout absorbs the brief lock held
        # at commit rather than failing with "database is locked".
        self._conn = sqlite3.connect(path, timeout=timeout)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def __enter__(self) -> "CorpusStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- writes ------------------------------------------------------------

    def upsert_ticket(self, ticket: dict[str, Any]) -> None:
        via = (ticket.get("via") or {}).get("channel")
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tickets (id, subject, status, created_at, updated_at,
                    requester_id, assignee_id, group_id, via_channel, tags,
                    satisfaction, raw)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    subject=excluded.subject,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    assignee_id=excluded.assignee_id,
                    group_id=excluded.group_id,
                    tags=excluded.tags,
                    satisfaction=excluded.satisfaction,
                    raw=excluded.raw
                """,
                (
                    ticket.get("id"),
                    ticket.get("subject"),
                    ticket.get("status"),
                    ticket.get("created_at"),
                    ticket.get("updated_at"),
                    ticket.get("requester_id"),
                    ticket.get("assignee_id"),
                    ticket.get("group_id"),
                    via,
                    json.dumps(ticket.get("tags") or []),
                    json.dumps(ticket.get("satisfaction_rating") or {}),
                    json.dumps(ticket),
                ),
            )

    def replace_comments(self, ticket_id: int, comments: list[dict[str, Any]]) -> None:
        """Replace a ticket's comments wholesale.

        Comments are immutable in Zendesk, but a re-export of an updated ticket
        should not duplicate them - replacing is simpler than diffing.
        """
        with self._tx() as conn:
            conn.execute("DELETE FROM comments WHERE ticket_id = ?", (ticket_id,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO comments
                    (id, ticket_id, author_id, public, created_at, body, clean_body)
                VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        c.get("id"),
                        ticket_id,
                        c.get("author_id"),
                        1 if c.get("public") else 0,
                        c.get("created_at"),
                        c.get("body"),
                        c.get("clean_body"),
                    )
                    for c in comments
                ],
            )

    def upsert_users(self, users: list[dict[str, Any]]) -> None:
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO users (id, name, email, role) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, email=excluded.email, role=excluded.role
                """,
                [(u.get("id"), u.get("name"), u.get("email"), u.get("role")) for u in users],
            )

    def set_state(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO export_state (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- reads -------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM export_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def ticket_ids(self) -> list[int]:
        return [r["id"] for r in self._conn.execute("SELECT id FROM tickets ORDER BY id")]

    def agent_ids(self) -> set[int]:
        rows = self._conn.execute(
            "SELECT id FROM users WHERE role IN ('agent','admin')"
        ).fetchall()
        return {r["id"] for r in rows}

    def set_ticket_themes(self, assignments: dict[int, str]) -> None:
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO ticket_themes (ticket_id, theme_key) VALUES (?,?) "
                "ON CONFLICT(ticket_id) DO UPDATE SET theme_key=excluded.theme_key",
                list(assignments.items()),
            )

    def theme_counts(self) -> dict[str, int]:
        return {
            r["theme_key"]: r["n"]
            for r in self._conn.execute(
                "SELECT theme_key, COUNT(*) AS n FROM ticket_themes "
                "GROUP BY theme_key ORDER BY n DESC"
            )
        }

    def theme_agent_replies(self) -> dict[str, int]:
        """Agent replies available per theme - the evidence behind each rule."""
        agent_ids = self.agent_ids()
        if not agent_ids:
            return {}
        placeholders = ",".join("?" * len(agent_ids))
        return {
            r["theme_key"]: r["n"]
            for r in self._conn.execute(
                f"SELECT t.theme_key, COUNT(*) AS n "
                f"FROM ticket_themes t "
                f"JOIN comments c ON c.ticket_id = t.ticket_id "
                f"WHERE c.author_id IN ({placeholders}) "
                f"AND TRIM(COALESCE(c.clean_body, '')) != '' "
                f"GROUP BY t.theme_key ORDER BY n DESC",
                tuple(sorted(agent_ids)),
            )
        }

    def requester(self, ticket_id: int) -> dict[str, str] | None:
        """Name and email of whoever raised a ticket, for order matching."""
        row = self._conn.execute(
            "SELECT u.name, u.email FROM tickets t "
            "JOIN users u ON u.id = t.requester_id WHERE t.id = ?",
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        return {"name": row["name"] or "", "email": row["email"] or ""}

    def comment_bodies(self) -> list[tuple[int, str]]:
        """(comment id, raw body) for every stored comment."""
        return [
            (r["id"], r["body"] or "")
            for r in self._conn.execute("SELECT id, body FROM comments")
        ]

    def update_clean_bodies(self, pairs: list[tuple[int, str]]) -> None:
        with self._tx() as conn:
            conn.executemany(
                "UPDATE comments SET clean_body = ? WHERE id = ?",
                [(clean, comment_id) for comment_id, clean in pairs],
            )

    def stats(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0])

        return {
            "tickets": count("SELECT COUNT(*) FROM tickets"),
            "comments": count("SELECT COUNT(*) FROM comments"),
            "public_comments": count("SELECT COUNT(*) FROM comments WHERE public = 1"),
            "users": count("SELECT COUNT(*) FROM users"),
            "solved_tickets": count(
                "SELECT COUNT(*) FROM tickets WHERE status IN ('solved','closed')"
            ),
        }
