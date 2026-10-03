# Zoho Inventory Connector for Razorpay Agent Studio

A private, **read-only** connector that lets an AI agent read a merchant's
**Zoho Inventory** data: sales orders, shipments, stock and customers.
It is built as an **MCP server** (Model Context Protocol), the standard way to
give tools to agents built on the Claude Agent SDK, which Agent Studio uses.

The connector links the two systems a merchant actually runs:

- **Razorpay** knows the payment (`pay_...`).
- **Zoho Inventory** knows the order, the stock and the shipment.

When an order is paid through Razorpay, the merchant stores the Razorpay payment
id in the Zoho order's *Reference#* field. The connector can then answer
questions like:

- "Customer paid with `pay_TjJrIOvtskFJU8` but has not received anything. Why?"
- "Which paid orders have not shipped for more than 3 days?"
- "Do we have proof of delivery for this disputed payment?"

## Demo

An agent (Claude, connected to this MCP server) answering three merchant
questions against the live Zoho account, then the test suite. About 60 seconds.

![Demo](docs/demo/demo.gif)

[Download as MP4](docs/demo/demo.mp4). The demo is reproducible: see [demo/](demo/)
(`ask.py` runs one question through `claude -p` with this server as its only
tool source; `demo.tape` is the [VHS](https://github.com/charmbracelet/vhs) script that recorded it).

## Contents

| Document | What it covers |
|---|---|
| This README | What it is, setup, how to run, how to test |
| [docs/tools.md](docs/tools.md) | Every tool: inputs, outputs, when the agent should use it |
| [docs/tool-spec.json](docs/tool-spec.json) | Machine-readable MCP tool specification (exported from the server) |
| [docs/agent-capabilities.md](docs/agent-capabilities.md) | What the agent can and cannot do |
| [docs/limitations.md](docs/limitations.md) | Assumptions, limitations, and the long-term fix |
| [docs/findings.md](docs/findings.md) | Zoho API behaviour verified against a live account, with evidence |

## Requirements checklist

| Requirement from the brief | Where it is |
|---|---|
| Read tickets, orders or inventory | Orders, fulfilment, stock and customers: 12 tools |
| Working OAuth or API-key flow | OAuth 2.0 authorization-code flow with refresh: [auth.py](src/zoho_inventory_connector/auth.py) |
| list / get / search primitives | `list_sales_orders`, `get_sales_order`, `search_sales_orders`, `search_items`, `get_item`, `search_customers`, `get_customer` |
| Rate-limit handling | Per-minute limiter, concurrency cap, daily budget, 429 handling: [rate_limit.py](src/zoho_inventory_connector/rate_limit.py), [client.py](src/zoho_inventory_connector/client.py) |
| MCP tool specification | [server.py](src/zoho_inventory_connector/server.py), [docs/tool-spec.json](docs/tool-spec.json) |
| What the agent can and cannot do | [docs/agent-capabilities.md](docs/agent-capabilities.md) |
| Setup, run, assumptions, limitations | This README and [docs/limitations.md](docs/limitations.md) |

## How it works

```
Agent (Agent Studio / Claude)
        │  MCP (stdio or streamable HTTP)
        ▼
server.py      12 read-only tools, input validation, structured errors
        ▼
service.py     business logic: payment-id lookup, fulfilment state,
               stock shortfall, removes personal data the agent does not need
        ▼
client.py      daily budget → per-minute limiter → max 5 concurrent → HTTP
               retries 429 / 5xx / timeouts, refreshes token on 401
        ▼
auth.py        OAuth tokens, stored locally (file mode 0600), auto-refresh
        ▼
Zoho Inventory API  (www.zohoapis.in/inventory/v1)
```

Design decisions:

- **Read-only by design.** The OAuth grant contains only `.READ` scopes, so even
  a bug or a prompt injection cannot change merchant data.
- **Small, stable outputs.** Zoho returns large payloads with phone numbers, PAN
  and addresses. Every record is reduced to the fields an agent needs.
- **Rate limits are handled before Zoho complains.** Going over Zoho's per-minute
  limit blocks the *whole organization* for 30 minutes (measured, see
  [findings](docs/findings.md#2-rate-limits--measured)), so the connector limits
  itself and fails fast instead of retrying into a block.
- **Errors tell the agent what to do next.** Each error has a code, a message,
  a `retryable` flag and a hint, for example "stop and tell the user".

## Setup

### 1. Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A Zoho Inventory account (the free plan works). This project used the
  **India** data center (`zoho.in`).
- Node.js, only if you want to use MCP Inspector for testing

### 2. Create a Zoho OAuth client

1. Open the Zoho API Console for your data center (India: <https://api-console.zoho.in>).
2. **Add Client → Server-based Applications**.
3. Set **Authorized Redirect URI** to `http://localhost:8765/oauth/callback`.
4. Copy the **Client ID** and **Client Secret**.
5. In Zoho Inventory, open **Settings → Organization Profile** and copy the **Organization ID**.

### 3. Configure

```bash
git clone https://github.com/vinu0404/razorpay.git
cd razorpay
uv sync
cp .env.example .env      # then fill in the values from step 2
```

`.env` and the token folder `.tokens/` are git-ignored. No credentials are stored in this repository.

### 4. Authorize (one time)

```bash
uv run zoho-connector auth login
```

A browser opens on Zoho's consent page listing only read permissions. After you
click **Accept**, the refresh token is saved to `.tokens/zoho_connector.json`.

```bash
uv run zoho-connector auth status    # check authorization
uv run zoho-connector auth logout    # revoke the token and delete it locally
```

### 5. (Optional) Load demo data

[scripts/seed_zoho_demo.py](scripts/seed_zoho_demo.py) creates the fictional demo
data used in this project: 5 items, 5 customers and 5 sales orders in different
states, each linked to a Razorpay **test-mode** payment id. It uses its own
separate login with write scopes; the connector itself never writes.

```bash
python3 scripts/seed_zoho_demo.py auth
python3 scripts/seed_zoho_demo.py seed
```

| Order | Razorpay payment (test mode) | State | Used for |
|---|---|---|---|
| SO-00001 | pay_TjJrWWgj7jAttQ | shipped | normal case |
| SO-00002 | pay_TjJrIOvtskFJU8 | paid, not shipped | "paid but not received" |
| SO-00003 | pay_TjJr1DLusM3dSE | paid, item out of stock | stock shortfall |
| SO-00004 | pay_TjJqSqc2PW0zCs | delivered | dispute evidence |
| SO-00005 | pay_TjJpRryFu6TZAu | paid 9 days ago, not shipped | delayed fulfilment |
| none | pay_TjJqkl40gFyOIK | payment failed | no order exists |

All names use `example.com` emails. There is no real customer data.

## Running the connector

### With MCP Inspector (quick manual test)

```bash
npx @modelcontextprotocol/inspector uv --directory "$(pwd)" run zoho-connector serve
```

Open the URL it prints, click **Connect**, then **Tools → List Tools**.

### With Claude Desktop or any MCP client

Add this to the client's MCP configuration
(Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json`).
Replace the path with the absolute path of this repository.

```json
{
  "mcpServers": {
    "zoho-inventory": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/razorpay", "run", "zoho-connector", "serve"]
    }
  }
}
```

`--directory` matters: the connector reads `.env` and `.tokens/` from that folder.

### Over HTTP (for a hosted agent)

```bash
uv run zoho-connector serve --transport streamable-http
```

This variant has no authentication in front of it. See
[limitations](docs/limitations.md) before exposing it beyond localhost.

### Example agent session

> **User:** A customer says they paid with pay_TjJrIOvtskFJU8 two days ago and got nothing.
>
> **Agent** calls `find_order_by_payment_id` → order SO-00002, Ravi Demo,
> Ceramic Coffee Mug Set, `fulfilment_state: awaiting_shipment`.
> Then `check_stock(["MUG-SET"])` → 14 available for new orders.
>
> **Agent:** The payment is linked to order SO-00002. It is confirmed but not
> packed or shipped yet, and the item is in stock, so nothing is blocking
> shipment. Recommend the warehouse ships it today and the customer is told it
> is on the way.

## Configuration

All settings come from environment variables or `.env`
([config.py](src/zoho_inventory_connector/config.py)).

| Variable | Default | Meaning |
|---|---|---|
| `ZOHO_DC` | `in` | Zoho data center (`com`, `in`, `eu`, `com.au`, `jp`, `ca`, `sa`, `com.cn`) |
| `ZOHO_ORG_ID` | required | Zoho Inventory organization id |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` | required | OAuth client credentials |
| `ZOHO_REDIRECT_URI` | `http://localhost:8765/oauth/callback` | Must match the API Console exactly |
| `RATE_LIMIT_PER_MINUTE` | `90` | Below Zoho's 100/min |
| `MAX_CONCURRENCY` | `5` | Zoho free-plan concurrency limit |
| `DAILY_REQUEST_BUDGET` | `900` | Below Zoho's free-plan 1000/day |
| `HTTP_MAX_RETRIES` | `4` | Attempts for 429 / 5xx / timeouts |
| `ENV` | `dev` | `prod` switches logs to JSON |
| `LOG_LEVEL` | `INFO` | Logs go to stderr; stdout is reserved for MCP |

## Tests

```bash
uv run pytest
```

72 tests, no network needed (Zoho is mocked with `respx`). They cover:

- **OAuth:** refresh before expiry, only one refresh when many calls run at once,
  Zoho's "error inside a 200 response", token file permissions.
- **Rate limits:** sliding window, shared cooldown after a 429, rolling daily
  budget that survives restarts, failing fast on Zoho's 30-minute block.
- **Client:** which errors are retried (429, 5xx, timeouts) and which are not (4xx),
  401 → one refresh, a missing scope never triggers a refresh, pagination.
- **Business logic:** exact payment-id match, duplicate orders, fulfilment states,
  stock shortfall, input validation, personal fields removed.
- **MCP layer:** all 12 tools are read-only, errors reach the agent as
  structured JSON, internal errors do not leak details.

Rate-limit behaviour was also measured against the live API:

```bash
uv run zoho-connector probe-rate-limit --calls 120 --concurrency 5
```

> This command bypasses the connector's own limiter on purpose and can get
> the organization blocked for 30 minutes. Results are in
> [docs/evidence/](docs/evidence/).

## Project structure

```
src/zoho_inventory_connector/
  config.py          settings from env / .env
  logging_config.py  structured logs to stderr, one id per tool call
  errors.py          typed errors with code, hint and retryable flag
  auth.py            OAuth flow, token storage, refresh
  rate_limit.py      per-minute window, cooldown, daily budget
  client.py          Zoho HTTP client: limits, retries, error mapping, pagination
  service.py         business logic and output shaping
  server.py          MCP server and tool definitions
  cli.py             zoho-connector command line
tests/               pytest suite (Zoho mocked)
scripts/             demo data seeding (writes; not part of the connector)
docs/                tool reference, capabilities, limitations, findings, evidence
```
