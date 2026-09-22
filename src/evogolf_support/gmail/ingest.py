"""Pull the online@ support history into the corpus.

Roughly half the support operation never reached Zendesk: twelve months of
customer conversations, most of them pre-sale - custom fitting bookings,
club specs, quotes - handled by hand in a Gmail inbox. The assistant learned
from the other half only, which is why it had never seen that voice or that
kind of question.

The team's own Gmail labels do the classification, so nothing here has to
guess what is a customer and what is a supplier invoice.

Read-only throughout. This reads a live customer-facing mailbox and must
never write to it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import time
from email.utils import parseaddr
from typing import Any, Iterator

import httpx

from ..corpus.clean import clean_body
from ..corpus.store import CorpusStore
from . import oauth

log = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# The labels the team maintains themselves. Everything else in that mailbox
# is supplier mail, payouts, invoices or platform noise.
CUSTOMER_LABELS = ["Customer Email Enquiries", "Custom Fitting Enquiries"]

# Gmail thread ids are hex and Zendesk ticket ids are small integers, so
# imported conversations live in a reserved range well clear of both. It is
# derived from the thread id rather than assigned, so re-running the import
# updates a conversation instead of duplicating it.
DONE_KEY = "gmail_imported_threads"

ID_BASE = 2_000_000_000
ID_SPAN = 1_000_000_000

_MAILBOX = re.compile(r"@evolutiongolf\.co\.uk$", re.IGNORECASE)


def thread_key(thread_id: str) -> int:
    digest = hashlib.blake2b(thread_id.encode(), digest_size=8).hexdigest()
    return ID_BASE + int(digest, 16) % ID_SPAN


def person_key(email: str) -> int:
    digest = hashlib.blake2b(email.lower().encode(), digest_size=8).hexdigest()
    return ID_BASE + int(digest, 16) % ID_SPAN


# Gmail answers a rate limit with 403, not 429, and the reason is only in
# the body. Treating every 403 as fatal stops the import; treating every 403
# as a rate limit would retry a genuine permission failure forever.
_RETRYABLE = {"ratelimitexceeded", "userratelimitexceeded", "backenderror",
              "quotaexceeded", "internalerror"}
MAX_RETRIES = 6
# Gmail allows 250 quota units per user per second and a thread fetch costs
# ten, so requests are spaced rather than fired as fast as they will go. The
# first run did the latter and was throttled after 55 conversations.
PACE_SECONDS = 0.12


def _retryable(response: httpx.Response) -> bool:
    if response.status_code == 429 or response.status_code >= 500:
        return True
    if response.status_code != 403:
        return False
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except Exception:                                   # noqa: BLE001
        return False
    return any((e.get("reason") or "").lower() in _RETRYABLE for e in errors)


def _get(path: str, token: str, **params: Any) -> dict[str, Any]:
    """GET from Gmail, backing off when throttled."""
    for attempt in range(MAX_RETRIES):
        response = httpx.get(
            f"{API}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or None,
            timeout=30.0,
        )
        if response.status_code < 400:
            time.sleep(PACE_SECONDS)
            return response.json()
        if not _retryable(response) or attempt == MAX_RETRIES - 1:
            response.raise_for_status()
        wait = min(2.0 ** attempt, 30.0) + random.uniform(0, 0.5)
        log.info("Gmail throttled on %s; waiting %.1fs", path, wait)
        time.sleep(wait)
    raise RuntimeError(f"Gave up on Gmail {path} after {MAX_RETRIES} attempts")


def label_ids(token: str, names: list[str]) -> dict[str, str]:
    """Map label names to ids, so a renamed label fails loudly rather than
    silently importing nothing."""
    found = {
        label.get("name"): label.get("id")
        for label in _get("labels", token).get("labels", [])
    }
    missing = [n for n in names if n not in found]
    if missing:
        raise RuntimeError(
            f"Gmail labels not found: {', '.join(missing)}. The import relies "
            "on the team's own labels; if they have been renamed, update "
            "CUSTOMER_LABELS."
        )
    return {n: found[n] for n in names}


def thread_ids(token: str, label_id: str) -> Iterator[str]:
    page: str | None = None
    while True:
        params: dict[str, Any] = {"labelIds": label_id, "maxResults": 100}
        if page:
            params["pageToken"] = page
        data = _get("threads", token, **params)
        for thread in data.get("threads", []):
            if thread.get("id"):
                yield thread["id"]
        page = data.get("nextPageToken")
        if not page:
            return


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    except Exception:                                   # noqa: BLE001
        return ""


def message_text(payload: dict[str, Any]) -> str:
    """The plain-text body, preferring text/plain over the HTML alternative."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode((payload.get("body") or {}).get("data"))
    parts = payload.get("parts") or []
    for part in parts:                                  # depth first, plain wins
        if part.get("mimeType") == "text/plain":
            text = _decode((part.get("body") or {}).get("data"))
            if text.strip():
                return text
    for part in parts:
        text = message_text(part)
        if text.strip():
            return text
    if mime == "text/html":
        return re.sub(r"<[^>]+>", " ", _decode((payload.get("body") or {}).get("data")))
    return ""


def header(message: dict[str, Any], name: str) -> str:
    for h in (message.get("payload") or {}).get("headers", []):
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def import_threads(store: CorpusStore, limit: int | None = None) -> dict[str, int]:
    """Import the labelled customer conversations. Returns a summary."""
    token = oauth.access_token(store)
    labels = label_ids(token, CUSTOMER_LABELS)

    # One synthetic agent account stands for the shared mailbox. Replies from
    # it must read as agent replies, or every conversation would look like a
    # customer talking to themselves and the evaluation would have nothing to
    # compare a draft against.
    agent_id = person_key("online@evolutiongolf.co.uk")
    store.upsert_users([{"id": agent_id, "name": "Evo Support Team",
                         "email": "online@evolutiongolf.co.uk", "role": "agent"}])

    # A run that stops half way - throttled, redeployed - must not start
    # again from the beginning. Threads already in are recorded and skipped.
    done = set(json.loads(store.get_state(DONE_KEY) or "[]"))
    seen: set[str] = set()
    result = {"threads": 0, "messages": 0, "skipped_empty": 0, "already_in": 0}
    for name, label_id in labels.items():
        for tid in thread_ids(token, label_id):
            if tid in seen:
                continue                                # a thread can carry both labels
            seen.add(tid)
            if tid in done:
                result["already_in"] += 1
                continue
            if limit is not None and result["threads"] >= limit:
                log.info("gmail/import stopped at the %s-thread limit", limit)
                return result
            try:
                added = _import_one(store, token, tid, name, agent_id)
            except Exception as exc:                    # noqa: BLE001
                log.warning("Could not import Gmail thread %s: %s", tid, exc)
                continue
            if added:
                result["threads"] += 1
                result["messages"] += added
            else:
                result["skipped_empty"] += 1
            done.add(tid)
            if (result["threads"] + result["skipped_empty"]) % 25 == 0:
                # Checkpoint as we go, so progress survives an interruption.
                store.set_state(DONE_KEY, json.dumps(sorted(done)))
                log.info("gmail/import progress: %s", result)
    store.set_state(DONE_KEY, json.dumps(sorted(done)))
    log.info("gmail/import: %s", result)
    return result


def _import_one(store: CorpusStore, token: str, thread_id: str,
                label: str, agent_id: int) -> int:
    thread = _get(f"threads/{thread_id}", token, format="full")
    messages = thread.get("messages") or []
    if not messages:
        return 0

    ticket_id = thread_key(thread_id)
    comments: list[dict[str, Any]] = []
    people: dict[int, dict[str, Any]] = {}
    requester_id = None

    for message in messages:
        sender = parseaddr(header(message, "From"))[1].lower()
        if not sender:
            continue
        ours = bool(_MAILBOX.search(sender))
        author_id = agent_id if ours else person_key(sender)
        if not ours:
            people.setdefault(author_id, {
                "id": author_id,
                "name": parseaddr(header(message, "From"))[0] or sender,
                "email": sender, "role": "end-user",
            })
            if requester_id is None:
                requester_id = author_id

        raw = message_text(message.get("payload") or {})
        cleaned = clean_body(raw)
        if not cleaned.strip():
            continue
        stamp = int(message.get("internalDate") or 0) / 1000
        comments.append({
            "id": ID_BASE + int(hashlib.blake2b(
                (message.get("id") or "").encode(), digest_size=8).hexdigest(), 16
            ) % ID_SPAN,
            "author_id": author_id,
            "public": True,
            "created_at": _iso(stamp),
            "body": raw,
            "clean_body": cleaned,
        })

    # A thread with nothing from a customer, or nothing from us, teaches the
    # assistant nothing: it needs a question and an answer to learn from.
    authors = {c["author_id"] for c in comments}
    if requester_id is None or agent_id not in authors or len(authors) < 2:
        return 0

    if people:
        store.upsert_users(list(people.values()))
    first = min(c["created_at"] for c in comments)
    last = max(c["created_at"] for c in comments)
    store.upsert_ticket({
        "id": ticket_id,
        "subject": header(messages[0], "Subject"),
        "status": "closed",
        "created_at": first,
        "updated_at": last,
        "requester_id": requester_id,
        "via": {"channel": "gmail_online"},
        "tags": ["gmail_import", label.lower().replace(" ", "_")],
        "gmail_thread_id": thread_id,
    })
    store.replace_comments(ticket_id, comments)
    return len(comments)


def _iso(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
