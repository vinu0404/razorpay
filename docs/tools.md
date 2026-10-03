# Tool Reference

The connector exposes 12 MCP tools. All of them are **read-only**
(`readOnlyHint: true`, `destructiveHint: false`). The exact JSON schemas the
agent sees are in [tool-spec.json](tool-spec.json), exported from the running
server.

## Quick map

| Tool | Type | Use it when |
|---|---|---|
| `connection_status` | status | Start of a session, or after auth errors |
| `find_order_by_payment_id` | get | You have a Razorpay `pay_...` id |
| `get_sales_order` | get | You know the order id or number |
| `get_fulfilment_status` | get | "Where is my order?", delivery proof |
| `list_sales_orders` | list | Browse orders by state, customer or date |
| `search_sales_orders` | search | You only have a fragment (name, part of a number) |
| `list_paid_unshipped_orders` | list | Find paid orders stuck before shipping |
| `search_items` | search / list | Find an item or its SKU |
| `get_item` | get | One item with full stock breakdown |
| `check_stock` | get (batch) | Stock for up to 10 SKUs at once |
| `search_customers` | search / list | Find a customer id |
| `get_customer` | get | One customer summary |

## Key terms

- **Razorpay payment id**: `pay_` followed by 14 letters or digits. Stored in the
  Zoho order's *Reference#* field.
- **"Paid" order**: an order whose reference number is a Razorpay payment id.
  Zoho's own `paid_status` field only tracks Zoho invoices, so it stays `unpaid`
  for Razorpay payments. It is returned as `zoho_paid_status` and should be
  ignored for payment questions.
- **`fulfilment_state`**: one value the connector derives from Zoho's two status
  fields:

  | `fulfilment_state` | Zoho `status` | Zoho `shipped_status` |
  |---|---|---|
  | `draft` | draft | – |
  | `awaiting_shipment` | confirmed | pending |
  | `partially_shipped` | – | partially_shipped |
  | `shipped` | shipped | shipped |
  | `delivered` | fulfilled | fulfilled |
  | `void` | void | – |

- **Stock fields**: Zoho tracks two kinds of stock. The connector reports
  *physical* stock, which changes when goods ship.

  | Field | Meaning |
  |---|---|
  | `physical_stock_available` | Units physically in the warehouse |
  | `committed_to_open_orders` | Units reserved by confirmed orders |
  | `available_for_new_orders` | Physical minus committed. Can be negative |
  | `shortfall` | How many units are missing for open orders (0 if none) |
  | `accounting_stock_on_hand` | Zoho's accounting stock, changes on invoices. Shown for reference |

---

## connection_status

Checks the connection and shows how much API budget is left.

**Input:** none

**Output:**
```json
{
  "connected": true,
  "organization": {"organization_id": "60090439438", "name": "bit", "plan": "PREMIUM TRIAL",
                   "currency": "INR", "time_zone": "Asia/Calcutta"},
  "data_center": "in",
  "granted_scopes": ["ZohoInventory.salesorders.READ", "..."],
  "rate_limits": {"per_minute_limit": 90, "calls_in_last_minute": 1, "daily_budget": 900,
                  "daily_budget_remaining": 899, "max_concurrency": 5,
                  "blocked_for_seconds": null, "zoho_reported_daily_remaining": 7026}
}
```

---

## find_order_by_payment_id

Finds the order(s) whose Reference# is this Razorpay payment id, with line items,
packages and tracking. **Exact match only.**

| Input | Type | Required | Notes |
|---|---|---|---|
| `payment_id` | string | yes | Must match `pay_` + 14 letters or digits |

**Output when found:** `found: true` and `orders: [<order detail>]`.
If two orders share the same payment id, a `warning` about a possible duplicate
order is added.

**Output when not found:**
```json
{
  "payment_id": "pay_TjJqkl40gFyOIK",
  "found": false,
  "orders": [],
  "guidance": "No Zoho sales order references this payment id. Possible causes: the payment failed or was never captured, ..."
}
```
`found: false` does **not** prove the payment failed. The agent should check
the payment in Razorpay before telling the customer anything.

---

## get_sales_order

Full detail of one order.

| Input | Type | Required | Notes |
|---|---|---|---|
| `salesorder_id` | string | one of the two | Zoho internal id |
| `salesorder_number` | string | one of the two | e.g. `SO-00002` |

**Output (order detail):**
```json
{
  "salesorder_id": "4260047000000038085",
  "salesorder_number": "SO-00002",
  "razorpay_payment_id": "pay_TjJrIOvtskFJU8",
  "reference_number": "pay_TjJrIOvtskFJU8",
  "customer_id": "4260047000000039011",
  "customer_name": "Ravi Demo",
  "order_date": "2026-10-02",
  "fulfilment_state": "awaiting_shipment",
  "total": 799.0,
  "currency": "INR",
  "order_status": "confirmed",
  "invoiced_status": "not_invoiced",
  "zoho_paid_status": "unpaid",
  "expected_shipment_date": null,
  "line_items": [{"item_id": "...", "sku": "MUG-SET", "name": "Ceramic Coffee Mug Set",
                  "quantity": 1.0, "quantity_packed": 0.0, "quantity_shipped": 0.0, "rate": 799.0}],
  "packages": [],
  "notes": "Paid via Razorpay (test mode) pay_TjJrIOvtskFJU8"
}
```

---

## get_fulfilment_status

Where an order is in fulfilment, with tracking details. Use it for "where is my
order?" and to collect delivery proof for a dispute.

| Input | Type | Required |
|---|---|---|
| `salesorder_id` | string | one of the two |
| `salesorder_number` | string | one of the two |

**Output:**
```json
{
  "salesorder_number": "SO-00004",
  "razorpay_payment_id": "pay_TjJqSqc2PW0zCs",
  "order_date": "2026-09-26",
  "days_since_order": 7,
  "fulfilment_state": "delivered",
  "line_items": ["..."],
  "packages": [{"package_number": "PKG-00004", "package_status": "delivered",
                "shipment_number": "SHP-00004", "shipment_status": "delivered",
                "carrier": "Demo Courier", "tracking_number": "TEST00004",
                "shipment_date": "2026-09-26", "delivered_date": null}],
  "evidence_available": true
}
```
`evidence_available` is `true` when at least one package has a tracking number.

---

## list_sales_orders

Pages through orders, newest first.

| Input | Type | Default | Notes |
|---|---|---|---|
| `fulfilment_state` | enum | none | One of the states listed above |
| `customer_id` | string | none | From `search_customers` |
| `date_from` | string | none | `YYYY-MM-DD`, order date on or after |
| `date_to` | string | none | `YYYY-MM-DD`, order date on or before |
| `page` | int ≥ 1 | 1 | |
| `per_page` | int 1–50 | 25 | |

**Output:** `orders` (order summaries), `page`, `per_page`, `has_more`.

When `fulfilment_state` is set, filtering happens in the connector, because Zoho's
status filter silently ignores most values (see [findings](findings.md#3-sales-order-filters)).
It then scans at most 1,000 recent orders (5 pages of 200) and adds a `note` saying so.

---

## search_sales_orders

Free-text search across order number, reference number and customer name.
Matching is **partial**, so confirm the right order with the user.

| Input | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | At least 2 characters |
| `limit` | int 1–50 | 10 | |

**Output:** `query`, `orders` (summaries), `note`.

---

## list_paid_unshipped_orders

Orders that carry a Razorpay payment id but have not shipped, ordered by how long
they have been waiting. Use it to find delays before they turn into complaints
or chargebacks.

| Input | Type | Default | Notes |
|---|---|---|---|
| `min_days_waiting` | int ≥ 0 | 0 | Only orders at least this many days old |

**Output:**
```json
{
  "as_of": "2026-10-03",
  "min_days_waiting": 3,
  "count": 1,
  "orders": [{"salesorder_number": "SO-00005", "razorpay_payment_id": "pay_TjJpRryFu6TZAu",
              "customer_name": "Neha Demo", "order_date": "2026-09-24",
              "fulfilment_state": "awaiting_shipment", "days_waiting": 9, "...": "..."}]
}
```

---

## search_items

Finds items and their SKUs. Without a query it lists all items.

| Input | Type | Default |
|---|---|---|
| `query` | string | none (list all) |
| `page` | int ≥ 1 | 1 |
| `per_page` | int 1–50 | 25 |

**Output:** `items` with `item_id`, `sku`, `name`, `status`, `rate`, `unit`,
`physical_stock_available`; plus `page`, `per_page`, `has_more`.

---

## get_item

One item with price and full stock breakdown.

| Input | Type | Required |
|---|---|---|
| `item_id` | string | one of the two |
| `sku` | string | one of the two (exact) |

**Output:** item fields plus the stock fields listed in *Key terms*.

---

## check_stock

Stock for several SKUs in one call.

| Input | Type | Required | Notes |
|---|---|---|---|
| `skus` | list of strings | yes | 1–10 exact SKUs. Duplicates are ignored |

**Output:**
```json
{
  "stock": [{"sku": "JKT-L", "name": "Denim Jacket (L)", "physical_stock_available": 0.0,
             "committed_to_open_orders": 1.0, "available_for_new_orders": -1.0,
             "shortfall": 1.0, "accounting_stock_on_hand": 0.0, "tracks_inventory": true}],
  "unknown_skus": ["NOPE-1"],
  "note": "Physical stock changes on shipment; accounting_stock_on_hand changes on invoices/bills."
}
```

---

## search_customers

Finds customers. Prefer `email` when you know it, because it is an exact match.

| Input | Type | Default |
|---|---|---|
| `query` | string | none |
| `email` | string | none |
| `limit` | int 1–50 | 10 |

**Output:** `customers` with `customer_id`, `name`, `company_name`, `email`,
`status`, `outstanding_receivable`, `currency`.
Phone numbers, addresses and tax ids (PAN) are **never** returned.

---

## get_customer

| Input | Type | Required |
|---|---|---|
| `customer_id` | string | yes |

**Output:** the same customer summary as `search_customers`.

---

## Errors

When a tool fails, the agent receives an MCP error result (`isError: true`)
containing this JSON:

```json
{
  "error": "RATE_LIMITED",
  "message": "Zoho has temporarily blocked API calls for this organization",
  "retryable": true,
  "hint": "Do not retry in a loop. Wait retry_after_seconds (if given) before calling Zoho tools again, and tell the user if it is long.",
  "details": {"retry_after_seconds": 1750},
  "correlation_id": "6b8f9f3c9e8f"
}
```

| Code | Meaning | What the agent should do |
|---|---|---|
| `VALIDATION_ERROR` | Bad input (wrong payment id format, bad date, too many SKUs) | Fix the input and call again |
| `NOT_FOUND` | Order, item or customer does not exist | Tell the user; try a search tool |
| `AUTH_REQUIRED` | Connector is not authorized or the token was revoked | Stop. A human must run `zoho-connector auth login` |
| `FORBIDDEN` | The OAuth grant is missing a scope | Stop. A human must re-authorize |
| `RATE_LIMITED` | Zoho rate limit hit; `retry_after_seconds` says how long | Wait, do not loop |
| `DAILY_BUDGET_EXHAUSTED` | Daily API budget used up | Stop and tell the user |
| `SERVICE_UNAVAILABLE` | Zoho unreachable or returning 5xx after retries | Try again later |
| `UPSTREAM_ERROR` | Zoho returned another error | Report it; do not retry the same call |
| `INTERNAL_ERROR` | Bug in the connector; details are only in the logs | Report the `correlation_id` |

`correlation_id` matches the server log line for that call, for debugging.
