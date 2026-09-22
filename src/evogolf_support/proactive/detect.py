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

# Ceiling on how many orders one sweep reads, so a busy six weeks cannot
# turn a routine check into an unbounded crawl of the order history.
MAX_ORDERS_SCANNED = 1000

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
query AtRiskOrders($query: String!, $first: Int!, $after: String) {
  orders(first: $first, after: $after, query: $query,
         sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
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


def fetch_orders(limit: int = MAX_ORDERS_SCANNED, window_days: int = 45,
                 page_size: int = 100) -> list[dict[str, Any]]:
    """Every paid order in the window, newest first, following pagination.

    One page was not enough. A single `first: 100` returned the *oldest*
    hundred orders in six weeks, which at this shop's volume is a wall of
    long-since-delivered history - and never the orders placed today that
    have not gone out yet. That is why the undispatched check reported zero
    even with its threshold at zero: the orders it exists to find were on a
    page nobody asked for.

    Newest first now, so if the cap is ever reached it drops the oldest
    orders rather than the ones a customer is currently waiting on.
    """
    if not shopify.configured():
        return []
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).date()
    search = f"created_at:>={since.isoformat()} AND financial_status:paid"

    orders: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(orders) < limit:
        want = min(page_size, limit - len(orders))
        data = shopify._post(  # noqa: SLF001
            AT_RISK_QUERY, {"query": search, "first": want, "after": cursor}
        )
        block = data.get("orders", {})
        orders.extend(block.get("nodes", []))
        if len(orders) >= limit:
            # Enforce the ceiling here rather than trusting the page size we
            # asked for to be the page size we get.
            del orders[limit:]
            break
        info = block.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not cursor:
            break
    log.info("sweep/scanned %s paid order(s) from the last %s days",
             len(orders), window_days)
    return orders


def status_summary(limit: int = MAX_ORDERS_SCANNED) -> dict[str, Any]:
    """What this store's orders actually look like. Diagnostic only.

    Reports the order-level fulfilment state as well as the courier
    statuses. The courier counts alone could never answer the question that
    mattered - whether any order has no fulfilment at all - because an order
    with nothing dispatched contributes no courier rows to count.
    """
    try:
        orders = fetch_orders(limit=limit)
    except Exception as exc:
        log.warning("Could not read orders for the status summary: %s", exc)
        return {}
    return summarise(orders)


def summarise(orders: list[dict[str, Any]]) -> dict[str, Any]:
    courier: dict[str, int] = {}
    fulfilment: dict[str, int] = {}
    undispatched = 0
    dates = []
    for order in orders:
        state = (order.get("displayFulfillmentStatus") or "UNKNOWN").upper()
        fulfilment[state] = fulfilment.get(state, 0) + 1
        rows = order.get("fulfillments") or []
        if not rows:
            undispatched += 1
        for f in rows:
            key = (f.get("displayStatus") or "UNKNOWN").upper()
            courier[key] = courier.get(key, 0) + 1
        when = _parse(order.get("createdAt"))
        if when:
            dates.append(when)
    return {
        "orders": len(orders),
        "fulfilment": fulfilment,
        "courier": courier,
        "no_fulfilment": undispatched,
        "oldest": min(dates).date().isoformat() if dates else "",
        "newest": max(dates).date().isoformat() if dates else "",
    }


def find_at_risk(
    now: dt.datetime | None = None,
    limit: int = MAX_ORDERS_SCANNED,
    unfulfilled_days: int | None = None,
    transit_days: int | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> list[AtRisk]:
    """Orders that warrant an unprompted message.

    A caller that has already fetched the orders can pass them in, so the
    preview does not crawl the shop twice to show the findings and the
    summary of what it looked at.

    The day thresholds can be overridden for a positive control: a detector
    that has never returned a result looks the same whether it is correct or
    quietly broken, so being able to lower the bar and watch it fire is worth
    having. Overrides are only reachable from the dry-run preview.
    """
    unfulfilled_after = (
        UNFULFILLED_WORKING_DAYS if unfulfilled_days is None else max(0, unfulfilled_days)
    )
    transit_after = (
        IN_TRANSIT_WORKING_DAYS if transit_days is None else max(0, transit_days)
    )
    if not shopify.configured():
        log.warning("Shopify is not configured - cannot look for delayed orders")
        return []

    now = now or dt.datetime.now(dt.timezone.utc)
    if orders is None:
        try:
            orders = fetch_orders(limit=limit)
        except Exception as exc:
            log.warning("Could not read orders for the delay sweep: %s", exc)
            return []

    # Does the courier feed actually reach this store? If not a single
    # fulfillment in six weeks has reached a terminal status, nothing is
    # updating them, every dispatched order will eventually look overdue,
    # and "still in transit" means nothing here. Say so and do not use it.
    summary = summarise(orders)
    statuses = summary["courier"]
    transit_is_reliable = bool(TERMINAL_STATUSES & set(statuses))
    log.info("sweep/order states: %s | courier: %s | no fulfilment: %s",
             summary["fulfilment"] or "none", statuses or "none",
             summary["no_fulfilment"])
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
            if age >= unfulfilled_after:
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
                if age >= transit_after:
                    out.append(AtRisk(
                        reason="stuck_in_transit",
                        detail=f"Shipped {age} working days ago, not yet delivered",
                        days=age, **common))
                break

    log.info("Delay sweep found %s order(s) at risk", len(out))
    return out
