import asyncio
import stat
import time
import urllib.parse

import httpx
import pytest

from zoho_inventory_connector.auth import TokenSet, ZohoOAuth
from zoho_inventory_connector.config import READ_ONLY_SCOPES
from zoho_inventory_connector.errors import AuthRequiredError, ServiceUnavailableError

from .conftest import TOKEN_URL


def expire(store, oauth: ZohoOAuth) -> None:
    tokens = store.load()
    tokens.expires_at = time.time() + 10  # inside the refresh skew
    store.save(tokens)
    oauth._tokens = tokens


def test_authorization_url_requests_only_read_scopes_and_offline_access(oauth):
    url = urllib.parse.urlparse(oauth.authorization_url("state-1"))
    q = urllib.parse.parse_qs(url.query)
    assert url.netloc == "accounts.zoho.in"
    assert q["scope"][0].split(",") == list(READ_ONLY_SCOPES)
    assert all(s.endswith(".READ") for s in READ_ONLY_SCOPES)
    assert q["access_type"] == ["offline"]
    assert q["state"] == ["state-1"]


async def test_valid_token_is_reused_without_network(oauth, zoho):
    route = zoho.post(TOKEN_URL)
    assert await oauth.access_token() == "valid-token"
    assert not route.called


async def test_expiring_token_is_refreshed_and_persisted(oauth, store, zoho):
    expire(store, oauth)
    zoho.post(TOKEN_URL).respond(json={"access_token": "new-token", "expires_in": 3600, "api_domain": "https://www.zohoapis.in"})
    assert await oauth.access_token() == "new-token"
    saved = store.load()
    assert saved.access_token == "new-token"
    assert saved.refresh_token == "refresh-1"  # Zoho doesn't rotate refresh tokens


async def test_concurrent_callers_trigger_a_single_refresh(oauth, store, zoho):
    expire(store, oauth)
    route = zoho.post(TOKEN_URL).respond(json={"access_token": "new-token", "expires_in": 3600})
    tokens = await asyncio.gather(*(oauth.access_token() for _ in range(5)))
    assert set(tokens) == {"new-token"}
    assert route.call_count == 1


async def test_force_refresh_skips_when_token_already_replaced(oauth, zoho):
    route = zoho.post(TOKEN_URL)
    assert await oauth.force_refresh("some-older-token") == "valid-token"
    assert not route.called


async def test_zoho_error_in_200_body_means_auth_required(oauth, store, zoho):
    expire(store, oauth)
    zoho.post(TOKEN_URL).respond(200, json={"error": "invalid_code"})
    with pytest.raises(AuthRequiredError):
        await oauth.access_token()


async def test_token_endpoint_throttling_is_retryable_not_auth(oauth, store, zoho):
    expire(store, oauth)
    zoho.post(TOKEN_URL).respond(
        400, json={"error_description": "You have made too many requests continuously", "error": "Access Denied"}
    )
    with pytest.raises(ServiceUnavailableError):
        await oauth.access_token()


async def test_missing_tokens_raise_auth_required(settings, http, tmp_path):
    from zoho_inventory_connector.auth import TokenStore

    oauth = ZohoOAuth(settings, http, TokenStore(tmp_path / "none.json"))
    with pytest.raises(AuthRequiredError) as err:
        await oauth.access_token()
    assert "auth login" in err.value.hint


async def test_exchange_code_stores_tokens_owner_only(oauth, settings, zoho):
    zoho.post(TOKEN_URL).respond(json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    await oauth.exchange_code("code-1")
    mode = stat.S_IMODE(settings.token_file.stat().st_mode)
    assert mode == 0o600


async def test_token_store_survives_corrupt_file(settings, http):
    settings.token_file.write_text("{not json")
    from zoho_inventory_connector.auth import TokenStore

    assert TokenStore(settings.token_file).load() is None


def test_tokenset_expiry_math():
    assert TokenSet("a", "r", expires_at=100).expires_in(40) == 60


async def test_network_failure_is_service_unavailable(oauth, store, zoho):
    expire(store, oauth)
    zoho.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ServiceUnavailableError):
        await oauth.access_token()
