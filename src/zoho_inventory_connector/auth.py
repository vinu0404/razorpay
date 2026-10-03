"""Zoho OAuth 2.0 (authorization-code flow, server-based client).

- `run_login_flow` is the one-time, human-in-the-loop consent step (CLI only).
- `ZohoOAuth.access_token()` is what the API client calls: it returns a cached
  token and refreshes it shortly before expiry. Refreshes are serialized with a
  lock so concurrent tool calls trigger at most one refresh.
- Tokens persist to a 0600 file so restarts don't burn Zoho's limited
  refresh-token quota.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from .config import READ_ONLY_SCOPES, Settings
from .errors import AuthRequiredError, ServiceUnavailableError, ValidationError
from .logging_config import get_logger

logger = get_logger(__name__)

# Refresh this many seconds before Zoho's stated expiry.
EXPIRY_SKEW_SECONDS = 120


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    api_domain: str | None = None
    scope: str | None = None

    def expires_in(self, now: float) -> float:
        return self.expires_at - now


class TokenStore:
    """JSON file with owner-only permissions. Written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        try:
            return TokenSet(**json.loads(self.path.read_text()))
        except (ValueError, TypeError):
            logger.warning("token file unreadable; re-login required", extra={"path": str(self.path)})
            return None

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(tokens), fh)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class ZohoOAuth:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        store: TokenStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.http = http
        self.store = store
        self.clock = clock
        self._tokens: TokenSet | None = store.load()
        self._lock = asyncio.Lock()

    # ---------- consent ----------

    def authorization_url(self, state: str) -> str:
        params = {
            "scope": ",".join(READ_ONLY_SCOPES),
            "client_id": self.settings.zoho_client_id,
            "response_type": "code",
            "redirect_uri": self.settings.zoho_redirect_uri,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",
            "state": state,
        }
        return f"{self.settings.accounts_url}/oauth/v2/auth?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str) -> TokenSet:
        data = await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.zoho_redirect_uri,
        })
        if "refresh_token" not in data:
            raise AuthRequiredError("Zoho did not return a refresh token; re-run login with consent")
        tokens = self._token_set(data, refresh_token=data["refresh_token"])
        self._tokens = tokens
        self.store.save(tokens)
        return tokens

    # ---------- runtime ----------

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    async def access_token(self) -> str:
        tokens = self._require_tokens()
        if tokens.expires_in(self.clock()) > EXPIRY_SKEW_SECONDS:
            return tokens.access_token
        async with self._lock:
            tokens = self._require_tokens()
            if tokens.expires_in(self.clock()) > EXPIRY_SKEW_SECONDS:
                return tokens.access_token  # another task refreshed while we waited
            return await self._refresh(tokens)

    async def force_refresh(self, rejected_token: str) -> str:
        """Called after a 401. Skips the refresh if another task already did it."""
        async with self._lock:
            tokens = self._require_tokens()
            if tokens.access_token != rejected_token:
                return tokens.access_token
            return await self._refresh(tokens)

    async def revoke(self) -> None:
        tokens = self._tokens or self.store.load()
        if tokens:
            resp = await self.http.post(
                f"{self.settings.accounts_url}/oauth/v2/token/revoke",
                data={"token": tokens.refresh_token},
            )
            logger.info("refresh token revoke requested", extra={"status": resp.status_code})
        self._tokens = None
        self.store.clear()

    # ---------- internals ----------

    def _require_tokens(self) -> TokenSet:
        if self._tokens is None:
            raise AuthRequiredError("Connector is not authorized with Zoho")
        return self._tokens

    async def _refresh(self, tokens: TokenSet) -> str:
        logger.info("refreshing Zoho access token")
        data = await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
        })
        # Zoho does not rotate refresh tokens; keep the existing one.
        new = self._token_set(data, refresh_token=tokens.refresh_token)
        self._tokens = new
        self.store.save(new)
        return new.access_token

    async def _token_request(self, form: dict[str, str]) -> dict:
        form = {
            **form,
            "client_id": self.settings.zoho_client_id,
            "client_secret": self.settings.zoho_client_secret.get_secret_value(),
        }
        try:
            resp = await self.http.post(f"{self.settings.accounts_url}/oauth/v2/token", data=form)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("Zoho accounts server unreachable") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        # Zoho reports OAuth failures as HTTP 200 with an "error" field.
        error = data.get("error") or (None if resp.is_success else f"http_{resp.status_code}")
        if error:
            logger.warning("Zoho token endpoint error", extra={"error": error, "status": resp.status_code})
            if "too many requests" in str(data).lower() or resp.status_code == 429:
                raise ServiceUnavailableError(
                    "Zoho is throttling token refreshes", hint="Wait a few minutes before retrying."
                )
            raise AuthRequiredError(f"Zoho rejected the token request ({error})")
        return data

    def _token_set(self, data: dict, refresh_token: str) -> TokenSet:
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=refresh_token,
            expires_at=self.clock() + float(data.get("expires_in", 3600)),
            api_domain=data.get("api_domain"),
            scope=data.get("scope"),
        )


# ---------- one-time interactive login (CLI) ----------

def _wait_for_callback(redirect_uri: str, timeout: float) -> dict[str, str]:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValidationError("Interactive login needs a localhost ZOHO_REDIRECT_URI")
    received: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            if url.path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Zoho authorization received. You can close this tab.</h3>")
            done.set()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise AuthRequiredError("Timed out waiting for Zoho consent")
    finally:
        server.shutdown()
    return received


async def run_login_flow(oauth: ZohoOAuth, *, open_browser: bool = True, timeout: float = 300) -> TokenSet:
    state = secrets.token_urlsafe(16)
    url = oauth.authorization_url(state)
    print("Open this URL to authorize the connector (read-only scopes):\n" + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    params = await asyncio.to_thread(_wait_for_callback, oauth.settings.zoho_redirect_uri, timeout)
    if "error" in params:
        raise AuthRequiredError(f"Zoho consent denied: {params['error']}")
    if not secrets.compare_digest(params.get("state", ""), state):
        raise AuthRequiredError("OAuth state mismatch; aborting login")
    # The authorization code is valid for only ~60 seconds: exchange immediately.
    return await oauth.exchange_code(params["code"])
