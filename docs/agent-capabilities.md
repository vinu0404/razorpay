# What the Agent Can and Cannot Do

This connector gives an Agent Studio agent **read-only** access to one merchant's
Zoho Inventory organization.

## The agent CAN

| Capability | Tool(s) |
|---|---|
| Find the order linked to a Razorpay payment id | `find_order_by_payment_id` |
| Read a full order: customer, items, quantities, totals | `get_sales_order` |
| See whether an order is waiting, shipped or delivered, with carrier and tracking number | `get_fulfilment_status` |
| Collect delivery evidence for a payment dispute | `find_order_by_payment_id`, `get_fulfilment_status` |
| List orders by fulfilment state, customer or date range | `list_sales_orders` |
| Search orders by customer name or part of an order number | `search_sales_orders` |
| Find paid orders that have not shipped, and how long they have waited | `list_paid_unshipped_orders` |
| Check physical stock and spot shortfalls that block orders | `check_stock`, `get_item` |
| Find items and SKUs | `search_items` |
| Find a customer and see their outstanding balance | `search_customers`, `get_customer` |
| Check the connection and remaining API budget | `connection_status` |

### Example jobs this supports

1. **"Paid but not received"**: payment id → order → fulfilment state → stock.
   The agent can explain *why* an order is stuck (not packed, out of stock) and
   recommend the next step.
2. **Dispute response**: payment id → order → shipment with tracking number and
   delivery status. This is the evidence a dispute reply needs.
3. **Fulfilment delay review**: list every paid order waiting more than N days,
   before customers complain or raise chargebacks.
4. **Replacement check**: can a replacement ship now? (`check_stock`)

## The agent CANNOT

| Limitation | Why |
|---|---|
| Create, edit, cancel or delete orders, items or customers | Read-only by design. The OAuth grant has only `.READ` scopes, so Zoho itself rejects any write |
| Ship, pack or mark orders delivered | Same: read-only |
| Issue refunds or change payments | Payments live in Razorpay, not Zoho, and this connector does not write anywhere |
| Confirm that a Razorpay payment succeeded | Zoho does not know Razorpay payment status. Use Razorpay's own API or MCP server for that |
| Find orders that do not have the payment id in Reference# | The link between the two systems is that field. Orders created without it are invisible to `find_order_by_payment_id` |
| See customer phone numbers, addresses or tax ids (PAN) | Removed on purpose to limit personal data exposure |
| Read invoices, bills, purchase orders, returns, warehouses or attachments | Not in scope for this version |
| Receive real-time updates | No webhooks. The agent only sees data when it calls a tool |
| See more than about 1,000 recent orders when filtering by state | The state filter runs in the connector over at most 5 pages of 200 orders, to protect the API budget |
| Access more than one Zoho organization | One organization per connector instance |
| Make unlimited calls | Capped at 90 calls/minute and 900 calls/day by default. Going over Zoho's limit blocks the whole organization for 30 minutes |

## Safety properties

- **Least privilege:** only read scopes are requested. A prompt-injected agent
  cannot change merchant data, because the token does not allow it.
- **Input validation before any API call:** malformed payment ids, dates or
  oversized requests are rejected without using API budget.
- **Exact matching for payment ids:** the connector re-checks Zoho's results, so
  a partial match can never return another customer's order.
- **Minimal personal data:** customer records include name and email only.
- **Errors with instructions:** every error tells the agent whether to retry,
  wait, or stop and ask a human.
- **Human decides actions:** the server instructions tell the agent to recommend
  actions, never to claim it performed one.
