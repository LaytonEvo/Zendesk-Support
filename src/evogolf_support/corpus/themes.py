"""Work out what the tickets are actually about, from tags and subjects.

This runs before any model is involved: Zendesk tags and subject lines already
carry the shape of the workload, and counting them costs nothing and needs no
API key. The ranking it produces decides which themes the voice-and-policy
mining should cover first, and how much evidence each one has behind it.

Output is aggregate counts only - no subjects, no message bodies - so it is
safe to log and to quote.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .store import CorpusStore

# Words that say nothing about the theme: grammar, the company's own name,
# and the reply/forward prefixes Zendesk puts in subject lines.
STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "cant", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "how", "i", "if", "in", "is", "it", "its", "just", "me", "my",
    "no", "not", "of", "on", "or", "our", "out", "please", "re", "fw", "fwd",
    "so", "than", "that", "the", "their", "them", "then", "there", "they",
    "this", "to", "up", "was", "we", "were", "what", "when", "which", "will",
    "with", "would", "you", "your", "yours", "am", "pm", "ltd", "evolution",
    "golf", "hi", "hello", "thanks", "thank", "enquiry", "query", "question",
    "help", "info", "information", "new", "get", "got", "need", "want",
}

# Themes worth reporting on explicitly, matched against tags and subjects.
# These are a starting lens, not a conclusion - the tag and keyword counts
# below are what actually decide the ranking.
THEME_PATTERNS = {
    "returns_refunds": r"return|refund|money back|send.{0,10}back|exchange",
    "delivery_tracking": r"deliver|dispatch|track|courier|dpd|parcel|shipping|postage|collection",
    "faulty_warranty": r"fault|broken|damage|warranty|repair|replace|defect",
    "order_status": r"order status|where is my|missing|not received|cancel",
    "product_sizing": r"size|sizing|fit|length|loft|shaft|spec",
    "stock_availability": r"stock|availability|available|back in|pre.?order",
    "payment_billing": r"payment|invoice|charge|discount|voucher|price",
}

_WORD = re.compile(r"[a-z][a-z'-]{2,}")


def report(store: CorpusStore, *, top: int = 20, min_word_count: int = 3) -> dict[str, Any]:
    """Rank themes by volume.

    ``min_word_count`` keeps one-off words out of the keyword list. Subject
    lines contain customer names, and a name that appears in a single ticket
    would otherwise be reported verbatim; requiring a word to recur across
    several tickets leaves brands, products and problems, which is what the
    ranking is for.
    """
    conn = store._conn  # noqa: SLF001
    agent_ids = store.agent_ids()

    rows = conn.execute(
        "SELECT id, subject, tags, status FROM tickets WHERE status != 'deleted'"
    ).fetchall()

    tag_counts: Counter[str] = Counter()
    word_counts: Counter[str] = Counter()
    theme_counts: Counter[str] = Counter()
    theme_ticket_ids: dict[str, list[int]] = {name: [] for name in THEME_PATTERNS}
    untagged = 0

    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        if tags:
            tag_counts.update(tags)
        else:
            untagged += 1

        subject = (row["subject"] or "").lower()
        word_counts.update(
            w for w in _WORD.findall(subject) if w not in STOPWORDS
        )

        haystack = f"{subject} {' '.join(tags).lower()}"
        for name, pattern in THEME_PATTERNS.items():
            if re.search(pattern, haystack):
                theme_counts[name] += 1
                theme_ticket_ids[name].append(row["id"])

    # How much agent-written evidence sits behind each theme - a theme with
    # few agent replies cannot support a confident policy rule.
    theme_evidence = {}
    for name, ids in theme_ticket_ids.items():
        if not ids:
            theme_evidence[name] = {"tickets": 0, "agent_replies": 0}
            continue
        placeholders = ",".join("?" * len(ids))
        agent_replies = 0
        if agent_ids:
            agent_ph = ",".join("?" * len(agent_ids))
            agent_replies = conn.execute(
                f"SELECT COUNT(*) FROM comments "
                f"WHERE ticket_id IN ({placeholders}) "
                f"AND author_id IN ({agent_ph}) "
                f"AND TRIM(COALESCE(clean_body, '')) != ''",
                (*ids, *sorted(agent_ids)),
            ).fetchone()[0]
        theme_evidence[name] = {
            "tickets": len(ids),
            "agent_replies": agent_replies,
        }

    matched = sum(1 for r in rows if any(
        re.search(p, f"{(r['subject'] or '').lower()} "
                     f"{' '.join(json.loads(r['tags'] or '[]')).lower()}")
        for p in THEME_PATTERNS.values()
    ))

    return {
        "tickets_considered": len(rows),
        "tickets_matching_no_theme": len(rows) - matched,
        "untagged_tickets": untagged,
        "themes_by_volume": dict(
            sorted(theme_evidence.items(),
                   key=lambda kv: kv[1]["tickets"], reverse=True)
        ),
        "top_tags": dict(tag_counts.most_common(top)),
        "top_subject_words": {
            word: count
            for word, count in word_counts.most_common(top)
            if count >= min_word_count
        },
    }
