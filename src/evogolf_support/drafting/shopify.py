"""Look up the live order behind a ticket.

Without this a draft can only say "[tracking number]" and leave the agent to
finish it, which on order-status tickets is most of the work. With it the
draft carries the real status, courier and tracking.

Order data is authoritative: the prompt is told to prefer it over anything
retrieved from the history.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from ..config import load_dotenv

log = logging.getLogger(__name__)

# Verified against the live schema; scopes needed are read_orders (plus
# read_all_orders for anything older than 60 days), read_customers, read_returns.
API_VERSION = "2026-07"

ORDER_QUERY = """
query SupportOrderLookup($query: String!, $first: Int!) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name
      createdAt
      cancelledAt
      displayFulfillmentStatus
      displayFinancialStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      customer { firstName lastName }
      shippingAddress { city countryCodeV2 }
      lineItems(first: 25) { nodes { title quantity sku variantTitle } }
      fulfillments(first: 10) {
        createdAt
        status
        trackingInfo(first: 5) { company number url }
      }
      refunds(first: 10) {
        createdAt
        totalRefundedSet { shopMoney { amount currencyCode } }
      }
      returns(first: 10) { nodes { status name } }
    }
  }
}
"""

# Order references as they appear in tickets: "order 27641", "#29103",
# "WEB-UK31018". Bare 4-6 digit runs are matched only when a cue word is near.
_ORDER_CUE = re.compile(
    r"(?:order|invoice|ref(?:erence)?|purchase)\D{0,12}?#?\s*([A-Z]{0,4}-?[A-Z]{0,4}\d{4,8})",
    re.IGNORECASE,
)
_HASH_NUMBER = re.compile(r"#\s?(\d{4,8})")


def configured() -> bool:
    load_dotenv()
    return bool(
        os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip()
        and os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
    )


def order_references(text: str) -> list[str]:
    """Order numbers mentioned in a ticket, most specific first."""
    found: list[str] = []
    for match in _HASH_NUMBER.finditer(text or ""):
        if match.group(1) not in found:
            found.append(match.group(1))
    for match in _ORDER_CUE.finditer(text or ""):
        ref = match.group(1).upper()
        if ref not in found:
            found.append(ref)
    return found[:3]


def _post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    load_dotenv()
    domain = os.environ["SHOPIFY_STORE_DOMAIN"].strip().replace("https://", "").strip("/")
    token = os.environ["SHOPIFY_ACCESS_TOKEN"].strip()
    version = os.environ.get("SHOPIFY_API_VERSION", API_VERSION).strip()

    response = httpx.post(
        f"https://{domain}/admin/api/{version}/graphql.json",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {payload['errors']}")
    return payload["data"]


def find_orders(*, reference: str | None = None, email: str | None = None,
                limit: int = 2) -> list[dict[str, Any]]:
    """Orders matching an order number or a customer email."""
    if not configured():
        return []
    if reference:
        search = f"name:*{reference}*"
    elif email:
        search = f"email:{email}"
    else:
        return []
    try:
        data = _post(ORDER_QUERY, {"query": search, "first": limit})
    except Exception as exc:
        log.warning("Shopify lookup failed for %r: %s", search, exc)
        return []
    return data.get("orders", {}).get("nodes", [])


def _money(node: Any) -> str:
    try:
        money = node["shopMoney"]
        return f"{money['currencyCode']} {money['amount']}"
    except (KeyError, TypeError):
        return "unknown"


def format_for_prompt(orders: list[dict[str, Any]]) -> str:
    """Render orders as compact, factual lines for the drafting prompt."""
    if not orders:
        return ""
    blocks = []
    for o in orders:
        name = (o.get("customer") or {})
        who = " ".join(x for x in [name.get("firstName"), name.get("lastName")] if x)
        lines = [
            f"Order {o.get('name')} placed {o.get('createdAt')}"
            + (f" by {who}" if who else ""),
            f"Payment: {o.get('displayFinancialStatus')}  "
            f"Fulfilment: {o.get('displayFulfillmentStatus')}  "
            f"Total: {_money(o.get('currentTotalPriceSet'))}",
        ]
        if o.get("cancelledAt"):
            lines.append(f"CANCELLED {o['cancelledAt']}")

        items = (o.get("lineItems") or {}).get("nodes", [])
        if items:
            lines.append("Items: " + "; ".join(
                f"{i.get('quantity')}x {i.get('title')}"
                + (f" ({i['variantTitle']})" if i.get("variantTitle") else "")
                for i in items
            ))

        for f in o.get("fulfillments") or []:
            for t in f.get("trackingInfo") or []:
                lines.append(
                    f"Shipped {f.get('createdAt')} [{f.get('status')}] via "
                    f"{t.get('company') or 'courier'} tracking {t.get('number') or 'n/a'}"
                )
            if not (f.get("trackingInfo") or []):
                lines.append(f"Fulfilment {f.get('createdAt')} [{f.get('status')}] - no tracking recorded")

        for r in o.get("refunds") or []:
            lines.append(f"Refunded {r.get('createdAt')}: {_money(r.get('totalRefundedSet'))}")
        for r in ((o.get("returns") or {}).get("nodes") or []):
            lines.append(f"Return {r.get('name')}: {r.get('status')}")

        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def context_for_ticket(text: str, *, email: str | None = None) -> str:
    """Best-effort order context for a ticket. Empty string when unavailable."""
    if not configured():
        return ""
    for ref in order_references(text):
        orders = find_orders(reference=ref)
        if orders:
            return format_for_prompt(orders)
    if email:
        return format_for_prompt(find_orders(email=email, limit=3))
    return ""
