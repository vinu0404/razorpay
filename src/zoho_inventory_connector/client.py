"""Read-only async client for the Zoho Inventory REST API.

Per request: daily budget -> per-minute window -> concurrency slot -> HTTP.
Retries (tenacity) cover 429, 5xx, timeouts and connection errors only; 4xx
client errors are never retried. A 401 triggers one token refresh outside the
retry budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .auth import ZohoOAuth
from .config import Settings
from .errors import (
    AuthRequiredError,
    DailyBudgetExhaustedError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UpstreamError,
)
from .logging_config import get_logger
from .rate_limit import DailyBudget, SlidingWindowLimiter

logger = get_logger(__name__)

# Zoho body codes worth mapping explicitly; anything else becomes UpstreamError.
_ZOHO_NOT_AUTHORIZED = {57}
_ZOHO_NOT_FOUND = {1002, 1004, 2006, 36004}


def _retriable_within(max_wait: float):
    def predicate(exc: BaseException) -> bool:
        if isinstance(exc, RateLimitedError):
            # A long Retry-After (Zoho's org block is 30 min) is not worth waiting out in-call.
            return exc.retry_after is None or exc.retry_after <= max_wait
        return isinstance(exc, ServiceUnavailableError)

    return predicate


class _Wait:
    """Honor Retry-After when Zoho sends it, otherwise exponential backoff with jitter."""

    def __init__(self, initial: float, maximum: float) -> None:
        self.maximum = maximum
        self.backoff = wait_exponential_jitter(initial=initial, max=maximum, jitter=1)

    def __call__(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RateLimitedError) and exc.retry_after is not None:
            return min(exc.retry_after, self.maximum)
        return self.backoff(state)


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "retrying Zoho request",
        extra={
            "attempt": state.attempt_number,
            "wait_seconds": round(state.next_action.sleep, 2) if state.next_action else None,
            "error": type(exc).__name__,
        },
    )


class ZohoInventoryClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        oauth: ZohoOAuth,
        limiter: SlidingWindowLimiter,
        budget: DailyBudget,
    ) -> None:
        self.settings = settings
        self.http = http
        self.oauth = oauth
        self.limiter = limiter
        self.budget = budget
        self._slots = asyncio.Semaphore(settings.max_concurrency)
        # Zoho's own daily quota, from x-rate-limit-* headers on the latest response.
        self.server_quota: dict[str, Any] | None = None

    @property
    def base_url(self) -> str:
        # Prefer the api_domain Zoho returned with the token (matches the account's DC).
        tokens = self.oauth.tokens
        if tokens and tokens.api_domain:
            return f"{tokens.api_domain.rstrip('/')}/inventory/v1"
        return self.settings.api_base_url

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.http_max_retries),
            wait=_Wait(self.settings.retry_wait_min, self.settings.retry_wait_max),
            retry=retry_if_exception(_retriable_within(self.settings.retry_wait_max)),
            before_sleep=_log_retry,
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._get_once(path, params or {})
        raise AssertionError("unreachable")  # pragma: no cover

    async def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        *,
        per_page: int = 200,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield rows across pages until has_more_page is false or max_pages is hit."""
        max_pages = max_pages or self.settings.max_scan_pages
        for page in range(1, max_pages + 1):
            body = await self.get(path, {**(params or {}), "page": page, "per_page": per_page})
            for row in body.get(key, []):
                yield row
            if not body.get("page_context", {}).get("has_more_page"):
                return
        logger.warning("pagination stopped at max_pages", extra={"path": path, "max_pages": max_pages})

    async def _get_once(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._check_server_quota()
        self.budget.consume()
        await self.limiter.acquire()
        async with self._slots:
            token = await self.oauth.access_token()
            resp = await self._send(path, params, token)
            # 401 + code 57 is a missing scope: a new token would not help.
            if resp.status_code == 401 and _zoho_code(resp) not in _ZOHO_NOT_AUTHORIZED:
                token = await self.oauth.force_refresh(token)
                self.budget.consume()
                resp = await self._send(path, params, token)
        return self._handle(resp, path)

    async def _send(self, path: str, params: dict[str, Any], token: str) -> httpx.Response:
        query = {"organization_id": self.settings.zoho_org_id, **params}
        try:
            resp = await self.http.get(
                f"{self.base_url}{path}",
                params=query,
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                timeout=self.settings.http_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("Zoho request timed out") from exc
        except httpx.TransportError as exc:
            raise ServiceUnavailableError("Could not reach Zoho") from exc
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("zoho response", extra={"path": path, "status": resp.status_code})
        self._record_server_quota(resp)
        return resp

    def _record_server_quota(self, resp: httpx.Response) -> None:
        h = resp.headers
        try:
            if "x-rate-limit-remaining" in h:
                self.server_quota = {
                    "limit": int(h["x-rate-limit-limit"]) if "x-rate-limit-limit" in h else None,
                    "remaining": int(h["x-rate-limit-remaining"]),
                    "resets_at": time.time() + int(h.get("x-rate-limit-reset", 0)),
                }
        except ValueError:
            pass

    def _check_server_quota(self) -> None:
        q = self.server_quota
        if q and q["remaining"] <= 0 and time.time() < q["resets_at"]:
            raise DailyBudgetExhaustedError(q["limit"] or 0, int(q["resets_at"] - time.time()))

    def _handle(self, resp: httpx.Response, path: str) -> dict[str, Any]:
        try:
            body: dict[str, Any] = resp.json()
        except ValueError:
            body = {}
        status = resp.status_code
        zoho_code = body.get("code")
        zoho_message = body.get("message", "")

        if status == 429:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            self.limiter.cool_down(retry_after if retry_after is not None else self.settings.rate_limit_cooldown_seconds)
            logger.warning(
                "Zoho returned 429",
                extra={"path": path, "retry_after_header": resp.headers.get("Retry-After"), "zoho_code": zoho_code},
            )
            raise RateLimitedError(zoho_message or "Zoho rate limit hit", retry_after=retry_after)
        if status >= 500:
            raise ServiceUnavailableError(f"Zoho server error ({status})")
        if status == 403 or zoho_code in _ZOHO_NOT_AUTHORIZED:
            raise ForbiddenError(
                "Connector lacks permission for this Zoho resource",
                hint="The OAuth grant may be missing a scope; a human must re-run `zoho-connector auth login`.",
                details={"zoho_code": zoho_code},
            )
        if status == 401:
            raise AuthRequiredError("Zoho rejected the access token after refresh")
        if status == 404 or zoho_code in _ZOHO_NOT_FOUND:
            raise NotFoundError("Zoho resource", path)
        if status >= 400 or (zoho_code not in (None, 0)):
            raise UpstreamError(
                f"Zoho error: {zoho_message or status}", details={"zoho_code": zoho_code, "status": status}
            )
        return body


def _zoho_code(resp: httpx.Response) -> int | None:
    try:
        return resp.json().get("code")
    except ValueError:
        return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None  # HTTP-date form; fall back to backoff

