"""Thin Zendesk Support API client with rate-limit handling.

Zendesk answers 429 with a Retry-After header telling us how many seconds to
wait; honouring it is the documented way to stay inside the plan's per-minute
budget. See https://developer.zendesk.com/api-reference/introduction/rate-limits/
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

from ..config import ZendeskConfig

log = logging.getLogger(__name__)

# A 429 should be rare; more than this in a row means something is wrong.
MAX_RETRIES = 5
# Fallback when a 429 arrives without a usable Retry-After header.
DEFAULT_BACKOFF_SECONDS = 30.0


class ZendeskError(RuntimeError):
    """A Zendesk API call failed in a way we cannot retry."""


class ZendeskNotFound(ZendeskError):
    """The record is gone - typically a ticket deleted since it was exported."""


class ZendeskClient:
    def __init__(self, config: ZendeskConfig | None = None, *, timeout: float = 30.0):
        self.config = config or ZendeskConfig.from_env()
        self._client = httpx.Client(
            base_url=self.config.base_url,
            auth=self.config.auth,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> "ZendeskClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """GET a Zendesk endpoint, retrying on 429 and transient 5xx."""
        for attempt in range(MAX_RETRIES):
            response = self._client.get(path, params=params or None)

            if response.status_code == 429:
                wait = _retry_after(response, DEFAULT_BACKOFF_SECONDS)
                log.warning("Rate limited on %s; sleeping %.0fs", path, wait)
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = 2.0 ** attempt
                log.warning(
                    "Zendesk %s on %s; retrying in %.0fs",
                    response.status_code,
                    path,
                    wait,
                )
                time.sleep(wait)
                continue

            if response.status_code == 401:
                raise ZendeskError(
                    "Zendesk rejected the credentials (401). Check "
                    "ZENDESK_EMAIL and ZENDESK_API_TOKEN, and that API token "
                    "access is enabled in Admin Center."
                )

            if response.status_code == 404:
                raise ZendeskNotFound(f"Zendesk 404 on {path}")

            if response.status_code >= 400:
                raise ZendeskError(
                    f"Zendesk {response.status_code} on {path}: {response.text[:400]}"
                )

            return response.json()

        raise ZendeskError(f"Gave up on {path} after {MAX_RETRIES} retries")

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to Zendesk, retrying on 429 and transient 5xx."""
        for attempt in range(MAX_RETRIES):
            response = self._client.post(path, json=payload)

            if response.status_code == 429:
                wait = _retry_after(response, DEFAULT_BACKOFF_SECONDS)
                log.warning("Rate limited on POST %s; sleeping %.0fs", path, wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500:
                time.sleep(2.0 ** attempt)
                continue
            if response.status_code >= 400:
                raise ZendeskError(
                    f"Zendesk {response.status_code} on POST {path}: "
                    f"{response.text[:400]}"
                )
            return response.json()

        raise ZendeskError(f"Gave up on POST {path} after {MAX_RETRIES} retries")

    def create_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return self.post("/tickets.json", {"ticket": ticket}).get("ticket", {})

    def verify(self) -> dict[str, Any]:
        """Confirm credentials work and report who we are authenticated as."""
        return self.get("/users/me.json").get("user", {})

    def incremental_tickets(self, start_time: int, cursor: str | None = None) -> Iterator[dict[str, Any]]:
        """Yield pages from the cursor-based incremental ticket export.

        Cursor exports give consistent page sizes and are Zendesk's recommended
        way to walk the full ticket history. Each page carries an ``after_cursor``
        to resume from and an ``end_of_stream`` flag marking the end.
        """
        params: dict[str, Any] = (
            {"cursor": cursor} if cursor else {"start_time": start_time}
        )
        while True:
            page = self.get("/incremental/tickets/cursor.json", **params)
            yield page
            if page.get("end_of_stream", True):
                return
            after = page.get("after_cursor")
            if not after:
                return
            params = {"cursor": after}

    def ticket_comments(self, ticket_id: int) -> list[dict[str, Any]]:
        """All comments on a ticket, oldest first."""
        comments: list[dict[str, Any]] = []
        params: dict[str, Any] = {"page[size]": 100}
        path = f"/tickets/{ticket_id}/comments.json"
        while True:
            page = self.get(path, **params)
            comments.extend(page.get("comments", []))
            meta = page.get("meta") or {}
            if not meta.get("has_more"):
                return comments
            after = (meta.get("after_cursor") or "").strip()
            if not after:
                return comments
            params = {"page[size]": 100, "page[after]": after}

    def users(self, user_ids: list[int]) -> list[dict[str, Any]]:
        """Look up users in batches (used to label agents vs customers)."""
        found: list[dict[str, Any]] = []
        for i in range(0, len(user_ids), 100):
            batch = user_ids[i : i + 100]
            ids = ",".join(str(u) for u in batch)
            found.extend(self.get("/users/show_many.json", ids=ids).get("users", []))
        return found


def _retry_after(response: httpx.Response, default: float) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    try:
        # Clamp: a malformed or hostile header should not stall the export for hours.
        return min(max(float(raw), 1.0), 300.0)
    except ValueError:
        return default
