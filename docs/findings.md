# Zoho Inventory API — Verified Findings

Org on India data center (`zohoapis.in`), plan reported by the API as **PREMIUM TRIAL**
(reverts to Free after trial). Tested 2026-10-03 against seeded fictional data.
Raw evidence: [`evidence/`](evidence/).

## 1. Access and auth — CONFIRMED
- OAuth server-based client on `accounts.zoho.in`; `http://localhost:8765/oauth/callback` accepted as redirect URI.
- Connector grant contains only the 6 READ scopes. Zoho returns granted scopes **space-separated**.
- Access token lifetime 3600 s; `api_domain` returned with the token (`https://www.zohoapis.in`).

## 2. Rate limits — MEASURED
| Run | Calls | Concurrency | Elapsed | Result |
|---|---|---|---|---|
| [probe 1](evidence/rate_limit_probe.json) | 120 | 5 | 3.3 s | all 200, no 429 |
| [probe 2](evidence/rate_limit_probe_250.json) (immediately after) | 250 | 10 | 5.1 s | 215 × 200, 35 × 429 |

The 429 response:
- Body: `{"code": 44, "message": "For security reasons your organization has been blocked as it have exceeded the maximum number of requests per minute ..."}`
- **`Retry-After: 1800`** — the header *is* sent (third-party guides claim it is not), and the penalty is a **30-minute block of the whole organization**, not a short backoff.
- Every response carries daily-quota headers: `x-rate-limit-limit: 7500` (trial), `x-rate-limit-remaining`, `x-rate-limit-reset` (seconds to reset; reset lands at midnight IST, the org time zone).

Design consequences (implemented):
- Client-side per-minute limiter is essential, not optional: overshooting costs 30 minutes of downtime for every API consumer of the org.
- 429 with a long Retry-After is **not retried**; all further calls fail fast with `RATE_LIMITED` + seconds remaining instead of hanging or extending the block.
- `x-rate-limit-remaining` is tracked; calls stop at 0 with `DAILY_BUDGET_EXHAUSTED`.
- Defaults (90/min, 5 concurrent, 900/day) target the **Free** plan limits so the connector stays safe after the trial ends.

## 3. Sales-order filters
| Query | Result | Connector use |
|---|---|---|
| `reference_number=<full pay_ id>` | exact order | `find_order_by_payment_id` (plus exact re-check) |
| `reference_number=<partial>` | empty | exact-match filter |
| `search_text=<full or partial pay_ id>` | matching order | fuzzy search only |
| `search_text=<customer name>` | matching order | `search_sales_orders` |
| `date_start` / `date_end` | works | `list_sales_orders` date range |
| `customer_id` | works | `list_sales_orders` |
| `sort_column=date&sort_order=A` | works | oldest-first scans |
| `filter_by=Status.Shipped` | works | — |
| `filter_by=Status.Confirmed` | **silently ignored** (`applied_filter: Status.All`) | status filtered client-side |
| `search_text=<failed payment id>` | empty | demo "no order for failed payment" |

Pagination: `page_context` has `page`, `per_page` (default 200), `has_more_page`.

## 4. Status values
| Order state | `status` | `shipped_status` | Connector `fulfilment_state` |
|---|---|---|---|
| Confirmed, not shipped | `confirmed` | `pending` | `awaiting_shipment` |
| Shipped | `shipped` | `shipped` | `shipped` |
| Delivered | `fulfilled` | `fulfilled` | `delivered` |

`paid_status` stays `unpaid` for Razorpay-paid orders: Zoho only knows about Zoho invoices/payments. The connector treats a `pay_` reference number as the payment signal.

## 5. Stock fields
| Field | Meaning | Changes on |
|---|---|---|
| `stock_on_hand`, `available_stock` | accounting stock | invoices / bills |
| `actual_available_stock` | physical stock | shipments |
| `actual_committed_stock` | reserved by confirmed orders | order confirm |
| `actual_available_for_sale_stock` | physical − committed (can be negative) | both |

After shipping 1 T-shirt: `stock_on_hand` 20, `actual_available_stock` 19. Denim Jacket with 1 confirmed order and 0 stock: `actual_available_for_sale_stock` = −1. The connector reports the physical fields and a `shortfall`.

## 6. Error codes
| HTTP | Zoho code | Meaning | Connector error | Source |
|---|---|---|---|---|
| 429 | 44 | org blocked for exceeding per-minute limit | `RATE_LIMITED` (fail fast) | observed |
| 400 | 6 | mandatory field missing (package number when auto-numbering is off) | seed script only | observed |
| 401 | 57 | not authorized (missing scope) — refreshing the token cannot fix it | `FORBIDDEN`, no refresh | Zoho docs, not reproduced |
| 401 | 14 | invalid / expired OAuth token | one refresh, then `AUTH_REQUIRED` | Zoho docs, not reproduced |
