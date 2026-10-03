"""MCP server exposing read-only Zoho Inventory tools to an agent."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, ParamSpec

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .auth import TokenStore, ZohoOAuth
from .client import ZohoInventoryClient
from .config import Settings, get_settings
from .errors import AppError
from .logging_config import get_logger, new_correlation_id
from .rate_limit import DailyBudget, SlidingWindowLimiter
from .service import InventoryService

logger = get_logger(__name__)

INSTRUCTIONS = """\
Read-only access to a merchant's Zoho Inventory: sales orders, fulfilment
(packages/shipments), item stock and customers. Orders paid through Razorpay
carry the Razorpay payment id (pay_...) in their reference number.

Guidelines:
- Start from a Razorpay payment id with find_order_by_payment_id.
- These tools cannot create, change, cancel or refund anything. Recommend
  actions to the human; never claim an action was taken.
- Every call spends a shared daily API budget. Prefer specific lookups over
  broad listings and do not poll.
- On AUTH_REQUIRED or DAILY_BUDGET_EXHAUSTED errors, stop and tell the user.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

mcp = MCPServer(name="zoho-inventory", instructions=INSTRUCTIONS)

_service: InventoryService | None = None


def build_service(settings: Settings) -> InventoryService:
    http = httpx.AsyncClient()
    oauth = ZohoOAuth(settings, http, TokenStore(settings.token_file))
    client = ZohoInventoryClient(
        settings,
        http,
        oauth,
        SlidingWindowLimiter(settings.rate_limit_per_minute, max_wait=settings.retry_wait_max),
        DailyBudget(settings.daily_request_budget, settings.budget_file),
    )
    return InventoryService(client)


def get_service() -> InventoryService:
    global _service
    if _service is None:
        _service = build_service(get_settings())
    return _service


P = ParamSpec("P")


def _tool_boundary(fn: Callable[P, Awaitable[dict[str, Any]]]) -> Callable[P, Awaitable[dict[str, Any]]]:
    """Correlation id + timing log per call; AppError -> structured tool error."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        cid = new_correlation_id()
        start = time.perf_counter()
        outcome = "ok"
        try:
            return await fn(*args, **kwargs)
        except AppError as exc:
            outcome = exc.code.value
            raise ToolError(exc.to_json(cid)) from exc
        except Exception as exc:
            outcome = "INTERNAL_ERROR"
            logger.exception("unhandled tool error", extra={"tool": fn.__name__})
            raise ToolError(
                f'{{"error": "INTERNAL_ERROR", "message": "Unexpected connector error", "correlation_id": "{cid}"}}'
            ) from exc
        finally:
            logger.info(
                "tool call",
                extra={"tool": fn.__name__, "outcome": outcome, "duration_ms": round((time.perf_counter() - start) * 1000, 1)},
            )

    return wrapper


def tool(title: str) -> Callable[[Callable[P, Awaitable[dict[str, Any]]]], Callable[P, Awaitable[dict[str, Any]]]]:
    def register(fn: Callable[P, Awaitable[dict[str, Any]]]) -> Callable[P, Awaitable[dict[str, Any]]]:
        wrapped = _tool_boundary(fn)
        mcp.tool(title=title, annotations=READ_ONLY)(wrapped)
        return wrapped

    return register


OrderId = Annotated[str | None, Field(description="Zoho salesorder_id (long numeric string). Give this or salesorder_number.")]
OrderNumber = Annotated[str | None, Field(description="Human order number, e.g. SO-00002. Give this or salesorder_id.")]
FulfilmentState = Literal["draft", "awaiting_shipment", "partially_shipped", "shipped", "delivered", "void"]


# ---------------- tools ----------------

@tool("Connection status")
async def connection_status() -> dict[str, Any]:
    """Check the Zoho connection: organization, data center, granted scopes and
    remaining API budget. Use once at the start of a session or when other tools
    fail with auth errors. Do not call repeatedly."""
    return await get_service().connection_status()


@tool("Find order by Razorpay payment id")
async def find_order_by_payment_id(
    payment_id: Annotated[str, Field(description="Razorpay payment id, exactly as issued: 'pay_' + 14 characters.")],
) -> dict[str, Any]:
    """Find the Zoho sales order(s) whose reference number is this Razorpay payment id,
    with line items, packages and shipment tracking.

    Use first whenever the user gives a pay_ id ("customer paid but didn't get the
    order", disputes, refund questions). Exact match only. found=false means no order
    references this payment; it does NOT prove the payment failed. Check Razorpay
    before telling the customer anything."""
    return await get_service().find_order_by_payment_id(payment_id)


@tool("Get sales order")
async def get_sales_order(salesorder_id: OrderId = None, salesorder_number: OrderNumber = None) -> dict[str, Any]:
    """Full detail of one sales order: customer, Razorpay payment id, line items,
    packages, shipments, invoice status. Use when you already know the order.
    zoho_paid_status reflects Zoho invoices only, not Razorpay; ignore it for
    payment questions."""
    return await get_service().get_sales_order(salesorder_id, salesorder_number)


@tool("Get fulfilment status")
async def get_fulfilment_status(salesorder_id: OrderId = None, salesorder_number: OrderNumber = None) -> dict[str, Any]:
    """Where an order is in fulfilment: awaiting_shipment / partially_shipped /
    shipped / delivered, with days since order, carrier, tracking number and
    delivery date. Use for "where is my order" and to gather delivery proof for a
    dispute (evidence_available=true means tracking exists)."""
    return await get_service().get_fulfilment_status(salesorder_id, salesorder_number)


@tool("List sales orders")
async def list_sales_orders(
    fulfilment_state: Annotated[FulfilmentState | None, Field(description="Only orders in this state.")] = None,
    customer_id: Annotated[str | None, Field(description="Only this customer's orders (from search_customers).")] = None,
    date_from: Annotated[str | None, Field(description="Order date on/after, YYYY-MM-DD.")] = None,
    date_to: Annotated[str | None, Field(description="Order date on/before, YYYY-MM-DD.")] = None,
    page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    per_page: Annotated[int, Field(ge=1, le=50, description="Results per page (max 50).")] = 25,
) -> dict[str, Any]:
    """Page through sales orders, newest first, optionally filtered by fulfilment
    state, customer and order-date range. Returns summaries; call get_sales_order
    for detail. For the paid-but-not-shipped backlog use list_paid_unshipped_orders
    instead."""
    return await get_service().list_sales_orders(fulfilment_state, customer_id, date_from, date_to, page, per_page)


@tool("Search sales orders")
async def search_sales_orders(
    query: Annotated[str, Field(min_length=2, description="Text matched against order number, reference number and customer name.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max results.")] = 10,
) -> dict[str, Any]:
    """Free-text order search when you only have a fragment (customer name, part of
    an order number). Matching is partial and fuzzy, so confirm the right order with
    the user. For a full Razorpay payment id use find_order_by_payment_id."""
    return await get_service().search_sales_orders(query, limit)


@tool("List paid but unshipped orders")
async def list_paid_unshipped_orders(
    min_days_waiting: Annotated[int, Field(ge=0, description="Only orders placed at least this many days ago.")] = 0,
) -> dict[str, Any]:
    """Orders that carry a Razorpay payment id but have not shipped yet, oldest
    first, with days_waiting. Use for fulfilment-delay reviews and to pre-empt
    "paid but not received" complaints and chargebacks."""
    return await get_service().list_paid_unshipped_orders(min_days_waiting)


@tool("Search items")
async def search_items(
    query: Annotated[str | None, Field(description="Text matched against item name and SKU. Omit to list all.")] = None,
    page: Annotated[int, Field(ge=1)] = 1,
    per_page: Annotated[int, Field(ge=1, le=50)] = 25,
) -> dict[str, Any]:
    """Find catalogue items and their SKUs. Use to discover a SKU before
    check_stock or get_item."""
    return await get_service().search_items(query, page, per_page)


@tool("Get item")
async def get_item(
    item_id: Annotated[str | None, Field(description="Zoho item_id. Give this or sku.")] = None,
    sku: Annotated[str | None, Field(description="Exact SKU, e.g. JKT-L. Give this or item_id.")] = None,
) -> dict[str, Any]:
    """One item with price and stock breakdown (physical, committed, available for
    new orders, shortfall)."""
    return await get_service().get_item(item_id, sku)


@tool("Check stock")
async def check_stock(
    skus: Annotated[list[str], Field(min_length=1, max_length=10, description="Exact SKUs to check (max 10).")],
) -> dict[str, Any]:
    """Physical stock for several SKUs at once. available_for_new_orders below zero
    (shortfall > 0) means open orders already exceed stock: explains stuck orders
    and tells you whether a replacement can ship."""
    return await get_service().check_stock(skus)


@tool("Search customers")
async def search_customers(
    query: Annotated[str | None, Field(description="Name or company text.")] = None,
    email: Annotated[str | None, Field(description="Exact email; preferred when known.")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Find customers to get a customer_id for list_sales_orders. Returns name,
    email and outstanding balance only; phone numbers, addresses and tax ids are
    never returned."""
    return await get_service().search_customers(query, email, limit)


@tool("Get customer")
async def get_customer(
    customer_id: Annotated[str, Field(description="Zoho customer id from search_customers or an order.")],
) -> dict[str, Any]:
    """One customer's summary (name, email, outstanding receivable)."""
    return await get_service().get_customer(customer_id)
