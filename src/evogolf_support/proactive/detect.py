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

# Courier outcomes that need a message now, whatever the elapsed time.
FAILED_STATUSES = {"DELAYED", "ATTEMPTED_DELIVERY", "NOT_DELIVERED", "FAILURE"}
# Statuses that mean the parcel is still moving, so the clock applies.
MOVING_STATUSES = {"IN_TRANSIT", "CARRIER_PICKED_UP", "SUBMITTED", "CONFIRMED",
                   "LABEL_PRINTED", "LABEL_PURCHASED", "FULFILLED",
                   "MARKED_AS_FULFILLED", "OUT_FOR_DELIVERY"}
SETTLED_STATUSES = {"DELIVERED", "PICKED_UP", "CANCELED", "LABEL_VOIDED"}

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

    out: list[AtRisk] = []
    for order in data.get("orders", {}).get("nodes", []):
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
            if status in MOVING_STATUSES:
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
