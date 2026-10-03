"""Command line entry point: `zoho-connector <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .auth import TokenStore, ZohoOAuth, run_login_flow
from .config import get_settings
from .errors import AppError
from .logging_config import configure_logging


def _oauth(http: httpx.AsyncClient) -> ZohoOAuth:
    settings = get_settings()
    return ZohoOAuth(settings, http, TokenStore(settings.token_file))


async def _login(open_browser: bool) -> None:
    async with httpx.AsyncClient() as http:
        tokens = await run_login_flow(_oauth(http), open_browser=open_browser)
    print(f"Authorized. Scopes: {tokens.scope}. Token stored in {get_settings().token_file}")


async def _status() -> None:
    async with httpx.AsyncClient() as http:
        oauth = _oauth(http)
        if oauth.tokens is None:
            print("Not authorized. Run: zoho-connector auth login")
            return
        expires = oauth.tokens.expires_in(time.time())
        print(json.dumps({
            "authorized": True,
            "scopes": oauth.tokens.scope,
            "api_domain": oauth.tokens.api_domain,
            "access_token_expires_in_seconds": int(expires),
        }, indent=2))


async def _logout() -> None:
    async with httpx.AsyncClient() as http:
        await _oauth(http).revoke()
    print("Refresh token revoked and local token file removed.")


async def _probe_rate_limit(calls: int, concurrency: int, out: Path) -> None:
    """Deliberately exceed Zoho's per-minute limit, bypassing the connector's own
    limiter, and record exactly what Zoho returns. Spends `calls` from the daily quota."""
    settings = get_settings()
    async with httpx.AsyncClient() as http:
        oauth = _oauth(http)
        token = await oauth.access_token()
        api_domain = oauth.tokens.api_domain if oauth.tokens else None
        base = f"{api_domain.rstrip('/')}/inventory/v1" if api_domain else settings.api_base_url
        sem = asyncio.Semaphore(concurrency)
        results: list[dict] = []
        start = time.perf_counter()

        async def one(i: int) -> None:
            async with sem:
                resp = await http.get(
                    f"{base}/items",
                    params={"organization_id": settings.zoho_org_id, "per_page": 1},
                    headers={"Authorization": f"Zoho-oauthtoken {token}"},
                    timeout=30,
                )
                entry = {"i": i, "t": round(time.perf_counter() - start, 2), "status": resp.status_code}
                if resp.status_code != 200:
                    entry["headers"] = {k: v for k, v in resp.headers.items() if k.lower() not in ("set-cookie",)}
                    entry["body"] = resp.text[:500]
                results.append(entry)

        await asyncio.gather(*(one(i) for i in range(calls)))

    results.sort(key=lambda r: r["i"])
    first_429 = next((r for r in results if r["status"] == 429), None)
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "calls": calls,
        "concurrency": concurrency,
        "elapsed_seconds": round(time.perf_counter() - start, 2),
        "status_counts": dict(Counter(r["status"] for r in results)),
        "first_429_at_call": first_429["i"] if first_429 else None,
        "retry_after_header_present": bool(first_429 and any(k.lower() == "retry-after" for k in first_429["headers"])),
        "first_429_headers": first_429["headers"] if first_429 else None,
        "first_429_body": first_429["body"] if first_429 else None,
        "non_200_samples": [r for r in results if r["status"] != 200][:5],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("status_counts", "first_429_at_call", "retry_after_header_present")}, indent=2))
    print(f"Full evidence written to {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="zoho-connector", description="Read-only Zoho Inventory MCP connector")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Manage Zoho OAuth authorization").add_subparsers(dest="action", required=True)
    login = auth.add_parser("login", help="One-time browser consent (read-only scopes)")
    login.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    auth.add_parser("status", help="Show local authorization state")
    auth.add_parser("logout", help="Revoke the refresh token and delete local tokens")

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")

    probe = sub.add_parser("probe-rate-limit", help="Exceed Zoho's per-minute limit and record the 429 response")
    probe.add_argument("--calls", type=int, default=110)
    probe.add_argument("--concurrency", type=int, default=5)
    probe.add_argument("--out", type=Path, default=Path("docs/evidence/rate_limit_probe.json"))

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging("zoho-inventory-connector", settings.log_level, settings.env)

    try:
        if args.command == "serve":
            from .server import mcp

            mcp.run(transport=args.transport)
        elif args.command == "auth":
            action = {"login": lambda: _login(not args.no_browser), "status": _status, "logout": _logout}[args.action]
            asyncio.run(action())
        elif args.command == "probe-rate-limit":
            asyncio.run(_probe_rate_limit(args.calls, args.concurrency, args.out))
    except AppError as exc:
        print(exc.to_json(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
