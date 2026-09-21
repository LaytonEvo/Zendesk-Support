"""Find orders the customer has not written in about, but should hear from us.

Rule 9 of the settled policy requires flagging a delay before being chased.
Nothing did that: the assistant only answered tickets that already existed.
This closes that gap by reading Shopify directly.

Two signals, both agreed with the business:
  - paid but still unfulfilled beyond three working days
  - shipped but still in transit beyond five working days

Anything the courier has explicitly reported as failed is treated as at risk
immediately, because the customer usually knows before we do.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from ..drafting import shopify

log = logging.getLogger(__name__)

UNFULFILLED_WORKING_DAYS = 3
IN_TRANSIT_WORKING_DAYS = 5

# Fulfillment statuses split by who actually reported them.
#
# The distinction matters: several statuses only mean "we created the
# fulfillment and handed it over", and carry no information from the courier
# at all. Treating those as "in transit" makes every dispatched order look
# late once enough days pass, because nothing ever moves them on.
MERCHANT_STATUSES = {"SUBMITTED", "CONFIRMED", "LABEL_PRINTED", "LABEL_PURCHASED",
                     "FULFILLED", "MARKED_AS_FULFILLED"}

# Courier outcomes that need a message now, whatever the elapsed time.
FAILED_STATUSES = {"DELAYED", "ATTEMPTED_DELIVERY", "NOT_DELIVERED", "FAILURE"}
# The courier says it is genuinely en route, so the clock applies.
MOVING_STATUSES = {"IN_TRANSIT", "CARRIER_PICKED_UP", "OUT_FOR_DELIVERY"}
# Arrived or cancelled - nothing to chase.
SETTLED_STATUSES = {"DELIVERED", "PICKED_UP", "CANCELED", "LABEL_VOIDED",
                    "READY_FOR_PICKUP"}
# Reaching one of these proves the courier feed is actually reporting back.
TERMINAL_STATUSES = {"DELIVERED", "PICKED_UP"}

AT_RISK_QUERY = """
query AtRiskOrders($query: String!, $first: Int!) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: false) {
    nodes {
      id
      name
      createdAt
      email
      displayFinancialStatus
      displayFulfillmentStatus
      customer { firstName lastName }
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 10) { nodes { title quantity } }
      fulfillments(first: 5) {
        createdAt
        displayStatus
        trackingInfo(first: 3) { company number url }
      }
    }
  }
}
"""


@dataclass
class AtRisk:
    order_name: str
    order_id: str
    reason: str
    detail: str
    days: int
    email: str
    customer_name: str
    items: str
    tracking: str


def working_days_between(start: dt.datetime, end: dt.datetime) -> int:
    """Whole working days from start to end, weekends excluded.

    Bank holidays are not handled: a shop is not open on them either, so the
    count runs slightly hot around them rather than missing a late order.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start.date()
    last = end.date()
    while cursor < last:
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _describe(order: dict[str, Any]) -> tuple[str, str, str]:
    items = "; ".join(
        f"{i.get('quantity')}x {i.get('title')}"
        for i in (order.get("lineItems") or {}).get("nodes", [])
    )
    who = order.get("customer") or {}
    name = " ".join(x for x in [who.get("firstName"), who.get("lastName")] if x)
    tracking = ""
    for f in order.get("fulfillments") or []:
        for t in f.get("trackingInfo") or []:
            if t.get("number"):
                tracking = f"{t.get('company') or 'courier'} {t['number']}"
                break
    return items, name, tracking


def status_summary(limit: int = 100) -> dict[str, int]:
    """What fulfilment statuses this store actually produces. Diagnostic."""
    if not shopify.configured():
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=45)).date().isoformat()
    try:
        data = shopify._post(  # noqa: SLF001
            AT_RISK_QUERY,
            {"query": f"created_at:>={since} AND financial_status:paid", "first": limit},
        )
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for order in data.get("orders", {}).get("nodes", []):
        for f in order.get("fulfillments") or []:
            key = (f.get("displayStatus") or "UNKNOWN").upper()
            counts[key] = counts.get(key, 0) + 1
    return counts


def find_at_risk(now: dt.datetime | None = None, limit: int = 100) -> list[AtRisk]:
    """Orders that warrant an unprompted message."""
    if not shopify.configured():
        log.warning("Shopify is not configured - cannot look for delayed orders")
        return []

    now = now or dt.datetime.now(dt.timezone.utc)
    # Look back far enough to catch anything still open, but not the whole history.
    since = (now - dt.timedelta(days=45)).date().isoformat()
    search = f"created_at:>={since} AND financial_status:paid"

    try:
        data = shopify._post(AT_RISK_QUERY, {"query": search, "first": limit})  # noqa: SLF001
    except Exception as exc:
        log.warning("Could not read orders for the delay sweep: %s", exc)
        return []

    orders = data.get("orders", {}).get("nodes", [])

    # Does the courier feed actually reach this store? If not a single
    # fulfillment in six weeks has reached a terminal status, nothing is
    # updating them, every dispatched order will eventually look overdue,
    # and "still in transit" means nothing here. Say so and do not use it.
    statuses: dict[str, int] = {}
    for order in orders:
        for f in order.get("fulfillments") or []:
            key = (f.get("displayStatus") or "UNKNOWN").upper()
            statuses[key] = statuses.get(key, 0) + 1
    transit_is_reliable = bool(TERMINAL_STATUSES & set(statuses))
    log.info("sweep/fulfilment statuses seen: %s", statuses or "none")
    if not transit_is_reliable and statuses:
        log.warning(
            "No fulfilment has reached DELIVERED or PICKED_UP, so the courier "
            "is not reporting status back into Shopify. Skipping the in-transit "
            "check - every dispatched order would otherwise be flagged as late. "
            "Undispatched orders and explicit courier failures are still checked."
        )

    out: list[AtRisk] = []
    for order in orders:
        created = _parse(order.get("createdAt"))
        if created is None:
            continue
        items, name, tracking = _describe(order)
        common = {
            "order_name": order.get("name", ""),
            "order_id": order.get("id", ""),
            "email": order.get("email") or "",
            "customer_name": name,
            "items": items,
            "tracking": tracking,
        }
        fulfilment = (order.get("displayFulfillmentStatus") or "").upper()
        fulfillments = order.get("fulfillments") or []

        if fulfilment in ("UNFULFILLED", "PARTIALLY_FULFILLED") and not fulfillments:
            age = working_days_between(created, now)
            if age >= UNFULFILLED_WORKING_DAYS:
                out.append(AtRisk(
                    reason="not_dispatched",
                    detail=f"Paid {age} working days ago and still not dispatched",
                    days=age, **common))
            continue

        for f in fulfillments:
            status = (f.get("displayStatus") or "").upper()
            if status in SETTLED_STATUSES:
                continue
            if status in FAILED_STATUSES:
                out.append(AtRisk(
                    reason="delivery_problem",
                    detail=f"Courier reports {status.replace('_', ' ').lower()}",
                    days=working_days_between(_parse(f.get("createdAt")) or created, now),
                    **common))
                break
            if status in MERCHANT_STATUSES:
                # "We dispatched it" - no courier information, nothing to judge.
                break
            if status in MOVING_STATUSES:
                if not transit_is_reliable:
                    break
                shipped = _parse(f.get("createdAt")) or created
                age = working_days_between(shipped, now)
                if age >= IN_TRANSIT_WORKING_DAYS:
                    out.append(AtRisk(
                        reason="stuck_in_transit",
                        detail=f"Shipped {age} working days ago, not yet delivered",
                        days=age, **common))
                break

    log.info("Delay sweep found %s order(s) at risk", len(out))
    return out
