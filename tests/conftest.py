import time
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from zoho_inventory_connector.auth import TokenSet, TokenStore, ZohoOAuth
from zoho_inventory_connector.client import ZohoInventoryClient
from zoho_inventory_connector.config import Settings
from zoho_inventory_connector.rate_limit import DailyBudget, SlidingWindowLimiter
from zoho_inventory_connector.service import InventoryService

API = "https://www.zohoapis.in/inventory/v1"
TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
ORG = "123456"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        zoho_dc="in",
        zoho_org_id=ORG,
        zoho_client_id="test-client",
        zoho_client_secret="test-secret",
        token_file=tmp_path / "tokens.json",
        budget_file=tmp_path / "budget.json",
        rate_limit_per_minute=1000,
        daily_request_budget=1000,
        rate_limit_cooldown_seconds=0.01,
        http_max_retries=3,
        retry_wait_min=0.01,
        retry_wait_max=0.05,
    )


@pytest.fixture
def store(settings: Settings) -> TokenStore:
    s = TokenStore(settings.token_file)
    s.save(TokenSet(
        access_token="valid-token",
        refresh_token="refresh-1",
        expires_at=time.time() + 3600,
        api_domain="https://www.zohoapis.in",
        scope="ZohoInventory.salesorders.READ",
    ))
    return s


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def oauth(settings, http, store) -> ZohoOAuth:
    return ZohoOAuth(settings, http, store)


@pytest.fixture
def client(settings, http, oauth) -> ZohoInventoryClient:
    return ZohoInventoryClient(
        settings,
        http,
        oauth,
        SlidingWindowLimiter(settings.rate_limit_per_minute, max_wait=settings.retry_wait_max),
        DailyBudget(settings.daily_request_budget, settings.budget_file),
    )


@pytest.fixture
def service(client) -> InventoryService:
    return InventoryService(client, today=lambda: date(2026, 10, 3))


@pytest.fixture
def zoho():
    with respx.mock(assert_all_called=False) as router:
        yield router


def so_row(number: str, ref: str | None, *, status="confirmed", shipped="pending", day="2026-10-01", **extra):
    return {
        "salesorder_id": f"id-{number}",
        "salesorder_number": number,
        "reference_number": ref or "",
        "customer_id": "c1",
        "customer_name": "Asha Demo",
        "date": day,
        "status": status,
        "shipped_status": shipped,
        "total": 499.0,
        "currency_code": "INR",
        **extra,
    }


def page(key: str, rows: list, has_more: bool = False) -> dict:
    return {"code": 0, "message": "success", key: rows, "page_context": {"has_more_page": has_more}}
