"""Domain layer: Zoho Inventory reads shaped for an agent.

Responsibilities:
- Validate inputs before spending API budget.
- Work around Zoho filter quirks (status filters, partial search matches).
- Return small, stable dicts. Raw Zoho payloads are large and carry PII the
  agent does not need (phone, PAN, addresses), so every record is projected.

"Paid" in this connector means the order's reference_number holds a Razorpay
payment id (pay_...). Zoho's own paid_status only reflects Zoho invoices.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from .client import ZohoInventoryClient
from .errors import NotFoundError, ValidationError

PAYMENT_ID_RE = re.compile(r"^pay_[A-Za-z0-9]{14}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FULFILMENT_STATES = (
    "draft",
    "awaiting_shipment",
    "partially_shipped",
    "shipped",
    "delivered",
    "void",
)
MAX_STOCK_SKUS = 10


# ---------- projections ----------

def fulfilment_state(row: dict[str, Any]) -> str:
    """Single state derived from Zoho's status + shipped_status pair."""
    status = (row.get("status") or "").lower()
    shipped = (row.get("shipped_status") or "").lower()
    if status == "void":
        return "void"
    if status == "draft":
        return "draft"
    if shipped == "fulfilled" or status == "fulfilled":
        return "delivered"
    if shipped == "shipped":
        return "shipped"
    if shipped == "partially_shipped":
        return "partially_shipped"
    return "awaiting_shipment"


def _payment_id(reference: str | None) -> str | None:
    ref = (reference or "").strip()
    return ref if PAYMENT_ID_RE.match(ref) else None


def order_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "salesorder_id": row.get("salesorder_id"),
        "salesorder_number": row.get("salesorder_number"),
        "razorpay_payment_id": _payment_id(row.get("reference_number")),
        "reference_number": row.get("reference_number") or None,
        "customer_id": row.get("customer_id"),
        "customer_name": row.get("customer_name"),
        "order_date": row.get("date"),
        "fulfilment_state": fulfilment_state(row),
        "total": row.get("total"),
        "currency": row.get("currency_code"),
    }


def _package(pkg: dict[str, Any]) -> dict[str, Any]:
    shipment = pkg.get("shipment_order") or {}
    return {
        "package_number": pkg.get("package_number"),
        "package_status": pkg.get("status"),
        "shipment_number": pkg.get("shipment_number") or None,
        "shipment_status": pkg.get("shipment_status") or None,
        "carrier": pkg.get("carrier") or None,
        "tracking_number": pkg.get("tracking_number") or None,
        "shipment_date": pkg.get("shipment_date") or None,
        "delivered_date": shipment.get("shipment_delivered_date") or shipment.get("delivery_date") or None,
    }


def order_detail(so: dict[str, Any]) -> dict[str, Any]:
    return {
        **order_summary(so),
        "order_status": so.get("order_status"),
        "invoiced_status": so.get("invoiced_status"),
        "zoho_paid_status": so.get("paid_status"),
        "expected_shipment_date": so.get("shipment_date") or None,
        "line_items": [
            {
                "item_id": li.get("item_id"),
                "sku": li.get("sku"),
                "name": li.get("name"),
                "quantity": li.get("quantity"),
                "quantity_packed": li.get("quantity_packed"),
                "quantity_shipped": li.get("quantity_shipped"),
                "rate": li.get("rate"),
            }
            for li in so.get("line_items", [])
        ],
        "packages": [_package(p) for p in so.get("packages", [])],
        "notes": so.get("notes") or None,
    }


def item_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": row.get("item_id"),
        "sku": row.get("sku"),
        "name": row.get("name"),
        "status": row.get("status"),
        "rate": row.get("rate"),
        "unit": row.get("unit"),
        "physical_stock_available": row.get("actual_available_stock"),
    }


def stock_detail(item: dict[str, Any]) -> dict[str, Any]:
    for_sale = item.get("actual_available_for_sale_stock")
    return {
        "item_id": item.get("item_id"),
        "sku": item.get("sku"),
        "name": item.get("name"),
        "physical_stock_available": item.get("actual_available_stock"),
        "committed_to_open_orders": item.get("actual_committed_stock"),
        "available_for_new_orders": for_sale,
        "shortfall": max(-(for_sale or 0), 0),
        "accounting_stock_on_hand": item.get("stock_on_hand"),
        "tracks_inventory": item.get("track_inventory"),
    }


def customer_summary(row: dict[str, Any]) -> dict[str, Any]:
    # Deliberately excludes phone, mobile, PAN, addresses and social handles.
    return {
        "customer_id": row.get("contact_id"),
        "name": row.get("contact_name"),
        "company_name": row.get("company_name") or None,
        "email": row.get("email") or None,
        "status": row.get("status"),
        "outstanding_receivable": row.get("outstanding_receivable_amount"),
        "currency": row.get("currency_code"),
    }


# ---------- validation ----------

def _require_payment_id(value: str) -> str:
    value = (value or "").strip()
    if not PAYMENT_ID_RE.match(value):
        raise ValidationError(
            "payment_id must be a Razorpay payment id like pay_AbCdEf12345678",
            details={"payment_id": value},
        )
    return value


def _check_date(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not DATE_RE.match(value):
        raise ValidationError(f"{name} must be YYYY-MM-DD", details={name: value})
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} is not a real date", details={name: value}) from exc
    return value


def _page_args(page: int, per_page: int, max_per_page: int) -> tuple[int, int]:
    if page < 1:
        raise ValidationError("page must be >= 1")
    if not 1 <= per_page <= max_per_page:
        raise ValidationError(f"per_page must be between 1 and {max_per_page}")
    return page, per_page


def _one_of(**kwargs: str | None) -> tuple[str, str]:
    given = [(k, v.strip()) for k, v in kwargs.items() if v and v.strip()]
    if len(given) != 1:
        raise ValidationError(f"Provide exactly one of: {', '.join(kwargs)}")
    return given[0]


# ---------- service ----------

class InventoryService:
    def __init__(self, client: ZohoInventoryClient, today: Callable[[], date] = date.today) -> None:
        self.client = client
        self.settings = client.settings
        self.today = today

    # --- connection ---

    async def connection_status(self) -> dict[str, Any]:
        body = await self.client.get("/organizations")
        org = next(
            (o for o in body.get("organizations", []) if str(o.get("organization_id")) == self.settings.zoho_org_id),
            None,
        )
        tokens = self.client.oauth.tokens
        return {
            "connected": org is not None,
            "organization": {
                "organization_id": self.settings.zoho_org_id,
                "name": org.get("name") if org else None,
                "plan": org.get("plan_name") if org else None,
                "currency": org.get("currency_code") if org else None,
                "time_zone": org.get("time_zone") if org else None,
            },
            "data_center": self.settings.zoho_dc,
            "granted_scopes": re.split(r"[,\s]+", tokens.scope.strip()) if tokens and tokens.scope else None,
            "rate_limits": {
                "per_minute_limit": self.client.limiter.max_calls,
                "calls_in_last_minute": self.client.limiter.calls_in_window,
                "daily_budget": self.client.budget.limit,
                "daily_budget_remaining": self.client.budget.remaining,
                "max_concurrency": self.settings.max_concurrency,
                "blocked_for_seconds": round(self.client.limiter.cooldown_remaining) or None,
                "zoho_reported_daily_remaining": (self.client.server_quota or {}).get("remaining"),
            },
        }

    # --- sales orders ---

    async def find_order_by_payment_id(self, payment_id: str) -> dict[str, Any]:
        payment_id = _require_payment_id(payment_id)
        body = await self.client.get("/salesorders", {"reference_number": payment_id})
        # Zoho's filter is exact today, but re-check so a behaviour change can't
        # silently return the wrong customer's order.
        rows = [r for r in body.get("salesorders", []) if (r.get("reference_number") or "").strip() == payment_id]
        if not rows:
            return {
                "payment_id": payment_id,
                "found": False,
                "orders": [],
                "guidance": (
                    "No Zoho sales order references this payment id. Possible causes: the payment "
                    "failed or was never captured, the order was not created in Zoho, or the payment "
                    "id was recorded in a different field. Verify the payment status in Razorpay "
                    "before telling the customer anything."
                ),
            }
        orders = [order_detail((await self.client.get(f"/salesorders/{r['salesorder_id']}"))["salesorder"]) for r in rows]
        result: dict[str, Any] = {"payment_id": payment_id, "found": True, "orders": orders}
        if len(orders) > 1:
            result["warning"] = "Multiple orders reference the same payment id; possible duplicate order."
        return result

    async def get_sales_order(self, salesorder_id: str | None = None, salesorder_number: str | None = None) -> dict[str, Any]:
        so = await self._load_order(salesorder_id, salesorder_number)
        return order_detail(so)

    async def get_fulfilment_status(self, salesorder_id: str | None = None, salesorder_number: str | None = None) -> dict[str, Any]:
        so = await self._load_order(salesorder_id, salesorder_number)
        detail = order_detail(so)
        days_since = (self.today() - date.fromisoformat(so["date"])).days if so.get("date") else None
        return {
            "salesorder_number": detail["salesorder_number"],
            "razorpay_payment_id": detail["razorpay_payment_id"],
            "order_date": detail["order_date"],
            "days_since_order": days_since,
            "fulfilment_state": detail["fulfilment_state"],
            "line_items": detail["line_items"],
            "packages": detail["packages"],
            "evidence_available": any(p["tracking_number"] for p in detail["packages"]),
        }

    async def list_sales_orders(
        self,
        fulfilment_state_filter: str | None = None,
        customer_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        page, per_page = _page_args(page, per_page, self.settings.max_page_size)
        params = self._order_filters(customer_id, date_from, date_to)

        if fulfilment_state_filter is None:
            body = await self.client.get("/salesorders", {**params, "page": page, "per_page": per_page})
            return {
                "orders": [order_summary(r) for r in body.get("salesorders", [])],
                "page": page,
                "per_page": per_page,
                "has_more": bool(body.get("page_context", {}).get("has_more_page")),
            }

        # Zoho's filter_by silently ignores most status values, so filter here.
        if fulfilment_state_filter not in FULFILMENT_STATES:
            raise ValidationError(f"fulfilment_state must be one of {FULFILMENT_STATES}")
        matches = [
            order_summary(r)
            async for r in self.client.paginate("/salesorders", "salesorders", params)
            if fulfilment_state(r) == fulfilment_state_filter
        ]
        start = (page - 1) * per_page
        return {
            "orders": matches[start:start + per_page],
            "page": page,
            "per_page": per_page,
            "has_more": len(matches) > start + per_page,
            "note": f"Filtered client-side over at most {self.settings.max_scan_pages * 200} most recent orders.",
        }

    async def search_sales_orders(self, query: str, limit: int = 10) -> dict[str, Any]:
        query = (query or "").strip()
        if len(query) < 2:
            raise ValidationError("query must be at least 2 characters")
        _page_args(1, limit, self.settings.max_page_size)
        body = await self.client.get("/salesorders", {"search_text": query, "per_page": limit})
        return {
            "query": query,
            "orders": [order_summary(r) for r in body.get("salesorders", [])],
            "note": "Zoho search is partial/fuzzy across order number, reference and customer name. "
                    "Use find_order_by_payment_id for exact payment lookups.",
        }

    async def list_paid_unshipped_orders(self, min_days_waiting: int = 0) -> dict[str, Any]:
        if min_days_waiting < 0:
            raise ValidationError("min_days_waiting must be >= 0")
        today = self.today()
        orders = []
        async for row in self.client.paginate("/salesorders", "salesorders", {"sort_column": "date", "sort_order": "A"}):
            if not _payment_id(row.get("reference_number")):
                continue
            if fulfilment_state(row) not in ("awaiting_shipment", "partially_shipped"):
                continue
            days = (today - date.fromisoformat(row["date"])).days
            if days >= min_days_waiting:
                orders.append({**order_summary(row), "days_waiting": days})
        orders.sort(key=lambda o: o["days_waiting"], reverse=True)
        return {"as_of": today.isoformat(), "min_days_waiting": min_days_waiting, "count": len(orders), "orders": orders}

    # --- items / stock ---

    async def search_items(self, query: str | None = None, page: int = 1, per_page: int = 25) -> dict[str, Any]:
        page, per_page = _page_args(page, per_page, self.settings.max_page_size)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if query and query.strip():
            params["search_text"] = query.strip()
        body = await self.client.get("/items", params)
        return {
            "items": [item_summary(r) for r in body.get("items", [])],
            "page": page,
            "per_page": per_page,
            "has_more": bool(body.get("page_context", {}).get("has_more_page")),
        }

    async def get_item(self, item_id: str | None = None, sku: str | None = None) -> dict[str, Any]:
        field, value = _one_of(item_id=item_id, sku=sku)
        if field == "sku":
            value = await self._item_id_for_sku(value)
        item = (await self.client.get(f"/items/{value}"))["item"]
        return {**item_summary(item), **stock_detail(item)}

    async def check_stock(self, skus: list[str]) -> dict[str, Any]:
        cleaned = list(dict.fromkeys(s.strip() for s in skus if s and s.strip()))
        if not cleaned:
            raise ValidationError("Provide at least one SKU")
        if len(cleaned) > MAX_STOCK_SKUS:
            raise ValidationError(f"At most {MAX_STOCK_SKUS} SKUs per call")
        results, missing = [], []
        for sku in cleaned:
            try:
                item_id = await self._item_id_for_sku(sku)
            except NotFoundError:
                missing.append(sku)
                continue
            results.append(stock_detail((await self.client.get(f"/items/{item_id}"))["item"]))
        return {
            "stock": results,
            "unknown_skus": missing,
            "note": "Physical stock changes on shipment; accounting_stock_on_hand changes on invoices/bills.",
        }

    # --- customers ---

    async def search_customers(self, query: str | None = None, email: str | None = None, limit: int = 10) -> dict[str, Any]:
        _page_args(1, limit, self.settings.max_page_size)
        params: dict[str, Any] = {"contact_type": "customer", "per_page": limit}
        if email and email.strip():
            params["email"] = email.strip()
        elif query and query.strip():
            params["search_text"] = query.strip()
        body = await self.client.get("/contacts", params)
        return {"customers": [customer_summary(r) for r in body.get("contacts", [])]}

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        if not (customer_id or "").strip():
            raise ValidationError("customer_id is required")
        contact = (await self.client.get(f"/contacts/{customer_id.strip()}"))["contact"]
        return customer_summary(contact)

    # --- helpers ---

    def _order_filters(self, customer_id: str | None, date_from: str | None, date_to: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if customer_id:
            params["customer_id"] = customer_id
        if _check_date("date_from", date_from):
            params["date_start"] = date_from
        if _check_date("date_to", date_to):
            params["date_end"] = date_to
        if date_from and date_to and date_from > date_to:
            raise ValidationError("date_from must be on or before date_to")
        return params

    async def _load_order(self, salesorder_id: str | None, salesorder_number: str | None) -> dict[str, Any]:
        field, value = _one_of(salesorder_id=salesorder_id, salesorder_number=salesorder_number)
        if field == "salesorder_number":
            body = await self.client.get("/salesorders", {"search_text": value})
            row = next((r for r in body.get("salesorders", []) if r.get("salesorder_number") == value), None)
            if row is None:
                raise NotFoundError("Sales order", value)
            value = row["salesorder_id"]
        return (await self.client.get(f"/salesorders/{value}"))["salesorder"]

    async def _item_id_for_sku(self, sku: str) -> str:
        body = await self.client.get("/items", {"sku": sku})
        row = next((r for r in body.get("items", []) if r.get("sku") == sku), None)
        if row is None:
            raise NotFoundError("Item with SKU", sku, hint="Use search_items to find the right SKU.")
        return row["item_id"]
