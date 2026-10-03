import httpx
import pytest

from zoho_inventory_connector.errors import (
    DailyBudgetExhaustedError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UpstreamError,
)

from .conftest import API, ORG, TOKEN_URL, page


async def test_sends_org_id_and_oauth_header(client, zoho):
    route = zoho.get(f"{API}/items").respond(json=page("items", []))
    await client.get("/items", {"sku": "X"})
    req = route.calls.last.request
    assert req.url.params["organization_id"] == ORG
    assert req.url.params["sku"] == "X"
    assert req.headers["Authorization"] == "Zoho-oauthtoken valid-token"


async def test_429_is_retried_then_succeeds_and_cools_down(client, zoho):
    route = zoho.get(f"{API}/items").mock(side_effect=[
        httpx.Response(429, json={"code": 44, "message": "too many"}),
        httpx.Response(200, json=page("items", [{"sku": "A"}])),
    ])
    body = await client.get("/items")
    assert body["items"] == [{"sku": "A"}]
    assert route.call_count == 2
    assert client.limiter._cooldown_until > 0


async def test_retry_after_header_is_honoured(client, zoho):
    zoho.get(f"{API}/items").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0.02"}, json={}),
        httpx.Response(200, json=page("items", [])),
    ])
    await client.get("/items")


async def test_persistent_429_surfaces_rate_limited(client, zoho):
    route = zoho.get(f"{API}/items").respond(429, json={})
    with pytest.raises(RateLimitedError):
        await client.get("/items")
    assert route.call_count == client.settings.http_max_retries


async def test_5xx_and_timeouts_are_retried(client, zoho):
    route = zoho.get(f"{API}/items").mock(side_effect=[
        httpx.Response(503),
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json=page("items", [])),
    ])
    await client.get("/items")
    assert route.call_count == 3


async def test_persistent_5xx_surfaces_service_unavailable(client, zoho):
    zoho.get(f"{API}/items").respond(502)
    with pytest.raises(ServiceUnavailableError):
        await client.get("/items")


@pytest.mark.parametrize("status,body,exc", [
    (400, {"code": 2, "message": "Invalid value"}, UpstreamError),
    (404, {"code": 1002, "message": "missing"}, NotFoundError),
    (401, {"code": 57, "message": "not authorized"}, ForbiddenError),
    (200, {"code": 1002, "message": "does not exist"}, NotFoundError),
])
async def test_client_errors_are_not_retried(client, zoho, status, body, exc):
    route = zoho.get(f"{API}/items/1").respond(status, json=body)
    refresh = zoho.post(TOKEN_URL)
    with pytest.raises(exc):
        await client.get("/items/1")
    assert route.call_count == 1
    assert not refresh.called  # missing scope must not burn a token refresh


async def test_401_refreshes_token_once_and_resends(client, zoho):
    route = zoho.get(f"{API}/items").mock(side_effect=[
        httpx.Response(401, json={"code": 14, "message": "Invalid OAuth token"}),
        httpx.Response(200, json=page("items", [])),
    ])
    refresh = zoho.post(TOKEN_URL).respond(json={"access_token": "fresh", "expires_in": 3600})
    await client.get("/items")
    assert refresh.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Zoho-oauthtoken fresh"


async def test_401_twice_means_auth_required(client, zoho):
    zoho.get(f"{API}/items").respond(401, json={"code": 14, "message": "Invalid OAuth token"})
    zoho.post(TOKEN_URL).respond(json={"access_token": "fresh", "expires_in": 3600})
    from zoho_inventory_connector.errors import AuthRequiredError

    with pytest.raises(AuthRequiredError):
        await client.get("/items")


async def test_daily_budget_stops_calls_before_network(client, zoho):
    client.budget.limit = 1
    route = zoho.get(f"{API}/items").respond(json=page("items", []))
    await client.get("/items")
    with pytest.raises(DailyBudgetExhaustedError):
        await client.get("/items")
    assert route.call_count == 1


async def test_paginate_follows_has_more_page(client, zoho):
    route = zoho.get(f"{API}/salesorders").mock(side_effect=[
        httpx.Response(200, json=page("salesorders", [{"n": 1}], has_more=True)),
        httpx.Response(200, json=page("salesorders", [{"n": 2}], has_more=False)),
    ])
    rows = [r async for r in client.paginate("/salesorders", "salesorders")]
    assert rows == [{"n": 1}, {"n": 2}]
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]


async def test_paginate_respects_max_pages(client, zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [{"n": 1}], has_more=True))
    rows = [r async for r in client.paginate("/salesorders", "salesorders", max_pages=2)]
    assert len(rows) == 2


# Shape of the real response captured in docs/evidence/rate_limit_probe_250.json
ORG_BLOCKED = {
    "code": 44,
    "message": "For security reasons your organization has been blocked as it have exceeded "
               "the maximum number of requests per minute that can originate from an organization.",
}


async def test_org_block_fails_fast_without_retrying(client, zoho):
    route = zoho.get(f"{API}/items").respond(429, headers={"Retry-After": "1800"}, json=ORG_BLOCKED)
    with pytest.raises(RateLimitedError) as err:
        await client.get("/items")
    assert route.call_count == 1  # retrying into a 30 min block only extends the damage
    assert err.value.retry_after == 1800


async def test_calls_during_org_block_never_reach_zoho(client, zoho):
    route = zoho.get(f"{API}/items").respond(429, headers={"Retry-After": "1800"}, json=ORG_BLOCKED)
    with pytest.raises(RateLimitedError):
        await client.get("/items")
    with pytest.raises(RateLimitedError) as err:
        await client.get("/items")
    assert route.call_count == 1
    assert 1700 < err.value.retry_after <= 1800


async def test_zoho_quota_headers_stop_calls_at_zero(client, zoho):
    route = zoho.get(f"{API}/items").respond(
        json=page("items", []),
        headers={"x-rate-limit-limit": "7500", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "3600"},
    )
    await client.get("/items")
    assert client.server_quota["limit"] == 7500
    with pytest.raises(DailyBudgetExhaustedError):
        await client.get("/items")
    assert route.call_count == 1
