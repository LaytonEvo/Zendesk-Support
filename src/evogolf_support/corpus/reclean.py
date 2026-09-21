"""Re-apply the cleaning rules to comments already in the corpus.

The raw bodies are kept, so a fix to the stripping or redaction rules can be
applied to the whole corpus locally - no Zendesk calls, no re-export, and no
risk of the rate limit. The service does this automatically on boot when
CLEANER_VERSION has moved on from the value stored in the corpus.
"""

from __future__ import annotations

import logging

from .clean import CLEANER_VERSION, clean_body
from .store import CorpusStore

log = logging.getLogger(__name__)

VERSION_KEY = "cleaner_version"


def needs_reclean(store: CorpusStore) -> bool:
    stored = store.get_state(VERSION_KEY)
    return stored != str(CLEANER_VERSION)


def reclean(store: CorpusStore, *, redact_pii: bool = True) -> int:
    """Recompute clean_body for every comment. Returns how many changed."""
    bodies = store.comment_bodies()
    updates: list[tuple[int, str]] = []
    for comment_id, raw in bodies:
        updates.append((comment_id, clean_body(raw, redact_pii=redact_pii)))

    store.update_clean_bodies(updates)
    store.set_state(VERSION_KEY, str(CLEANER_VERSION))
    log.info(
        "Re-cleaned %s comments with cleaner version %s", len(updates), CLEANER_VERSION
    )
    return len(updates)
