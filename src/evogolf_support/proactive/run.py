"""Run the delay sweep end to end."""

from __future__ import annotations

import logging

from ..config import corpus_path
from ..corpus.store import CorpusStore
from .detect import find_at_risk
from .notify import (already_flagged, draft_for, flag_key, mark_flagged,
                     raise_ticket, send_digest)

log = logging.getLogger(__name__)

# A ceiling on tickets raised in one sweep. If a supplier problem makes forty
# orders late at once, that is a conversation to have with a person, not forty
# tickets raised automatically overnight.
MAX_PER_SWEEP = 15


def run_sweep(dry_run: bool = False) -> dict[str, int]:
    at_risk = find_at_risk()
    result = {"found": len(at_risk), "new": 0, "tickets": 0, "skipped_seen": 0}
    if not at_risk:
        log.info("sweep/done: nothing at risk")
        return result

    with CorpusStore(corpus_path()) as store:
        seen = already_flagged(store)
        fresh = [i for i in at_risk if flag_key(i) not in seen]
        result["skipped_seen"] = len(at_risk) - len(fresh)
        result["new"] = len(fresh)

        if len(fresh) > MAX_PER_SWEEP:
            log.warning(
                "%s orders are at risk, above the %s ceiling - raising none and "
                "flagging for a human. This usually means one upstream problem, "
                "not many separate ones.",
                len(fresh), MAX_PER_SWEEP,
            )
            send_digest([
                f"*{len(fresh)} orders are late* - above the automatic ceiling, "
                "so no tickets were raised. Likely one supplier or courier issue.",
                *[f"- {i.order_name}: {i.detail}" for i in fresh[:20]],
            ])
            return result

        lines, raised = [], set()
        for item in fresh:
            if dry_run:
                lines.append(f"- {item.order_name}: {item.detail} (dry run)")
                continue
            try:
                draft = draft_for(store, item)
            except Exception as exc:
                log.warning("Could not draft for %s: %s", item.order_name, exc)
                continue
            ticket_id = raise_ticket(item, draft)
            if ticket_id:
                result["tickets"] += 1
                raised.add(flag_key(item))
                lines.append(
                    f"- {item.order_name}: {item.detail} "
                    f"(ticket #{ticket_id}"
                    + (", needs a decision" if draft.hand_to_agent else "")
                    + ")"
                )

        if lines:
            send_digest(lines)
        if raised and not dry_run:
            # Only orders that actually reached a ticket are marked, so a
            # failure part-way through is retried rather than lost.
            mark_flagged(store, seen | raised)

    log.info("sweep/done: %s", result)
    return result
