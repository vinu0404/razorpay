# Assumptions, Limitations and Long-Term Fix

## Assumptions

1. **The Razorpay payment id is stored in the Zoho order's Reference# field.**
   This is the only link between the two systems. In production, the merchant's
   checkout or order-sync integration must write it there. If a merchant uses a
   different field (for example a custom field), the lookup needs a small change.
2. **One Zoho organization per connector instance.** The organization id comes
   from configuration.
3. **"Paid" means "has a Razorpay payment id".** The connector does not call
   Razorpay to confirm the payment was captured.
4. **The free-plan limits apply** (100 requests/minute, 1,000/day, 5 concurrent).
   The test organization was on a Premium trial with a 7,500/day limit, but the
   defaults are set for the free plan so the connector stays safe after the trial.
5. **Agent Studio supports MCP.** Agent Studio is built on the Claude Agent SDK,
   which supports MCP. Agent Studio itself is not publicly available, so the
   connector was tested through the MCP Python SDK (live calls to every tool
   against the Zoho account, plus the automated tests), not inside Agent Studio.

## Limitations

### Authentication
- **Single tenant.** Tokens are stored in one local file (`.tokens/`, mode 0600).
  There is no per-merchant token storage.
- **Tokens are not encrypted at rest.** File permissions are the only protection.
- **Login needs a browser on the same machine** (localhost redirect).
- Zoho allows at most **20 refresh tokens per user**. Logging in many times
  silently invalidates the oldest tokens.

### Rate limits
- **The limiter state is per process.** Two connector processes for the same
  organization would each allow 90 calls/minute and could together trigger
  Zoho's 30-minute block.
- **A Zoho block is not remembered across restarts.** After a restart during a
  block, the first call reaches Zoho, gets a 429 again, and only then fails fast.
- The daily budget uses a rolling 24-hour window, while Zoho resets at midnight
  in the organization's time zone. The connector is therefore stricter than
  needed, never looser. It also reads Zoho's `x-rate-limit-remaining` header and
  stops at 0.

### Data coverage
- Read-only: no writes of any kind.
- Covers sales orders, packages/shipments, items and customers only. No
  invoices, purchase orders, returns, warehouses or attachments.
- Filtering by fulfilment state scans at most 1,000 recent orders (configurable
  with `MAX_SCAN_PAGES`).
- Zoho's text search is fuzzy. `search_sales_orders` can return near matches, so
  exact lookups should use `find_order_by_payment_id` or `get_sales_order`.
- No caching: every tool call is a live API call, so data is always fresh but
  every call uses budget.
- No webhooks: the agent cannot react to changes in real time.

### Transport
- `serve --transport streamable-http` has **no authentication** in front of it.
  Only use it on localhost or behind an authenticating proxy.

### Verification gaps
- Zoho error codes `57` (missing scope) and `14` (invalid token) are handled
  according to Zoho's documentation but were not reproduced live.
- The rate-limit probe measured the block at concurrency 10 after an earlier
  burst. The exact threshold that triggers the block was not mapped further,
  because each test costs 30 minutes of downtime.

## Long-term fix (production design)

| Area | Today | Production |
|---|---|---|
| Hosting | Local process per merchant (stdio) | Hosted multi-tenant MCP service over streamable HTTP, with OAuth in front of it so Agent Studio authenticates to the connector |
| Token storage | Local 0600 file | Encrypted secret store (for example a KMS-backed vault), one token set per merchant, key rotation |
| Merchant onboarding | Developer runs `auth login` | "Connect Zoho" button in the merchant dashboard runs the same OAuth flow; region detected automatically |
| Rate limiting | In-process counters | Shared limiter per Zoho organization (for example Redis), so all workers share one budget and block state |
| Freshness and cost | Live API call every time | Webhooks or incremental sync into a local read model; tools read from the cache and only fall back to the API when needed |
| Payment link | Agent trusts Reference# | Join with Razorpay data (Payments API or Razorpay MCP server) to confirm capture status and detect orders missing a payment id |
| Writes | None | Separate write tools (for example "mark shipped", "add note") gated by a human approval step in Agent Studio, with an audit log |
| Coverage | Orders, shipments, items, customers | Add invoices, returns and warehouses, based on which agent jobs merchants actually use |
| Observability | Structured logs with a correlation id | Metrics per tool (latency, errors, budget use), alerts before quota runs out, traces |
| Distribution | Private connector | Listed Zoho Marketplace app so merchants can connect without creating their own OAuth client |
