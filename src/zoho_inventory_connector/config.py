"""Connector configuration. All values come from env vars / .env via Settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Read-only scopes. The connector never requests CREATE/UPDATE/DELETE.
READ_ONLY_SCOPES = (
    "ZohoInventory.salesorders.READ",
    "ZohoInventory.items.READ",
    "ZohoInventory.contacts.READ",
    "ZohoInventory.packages.READ",
    "ZohoInventory.shipmentorders.READ",
    "ZohoInventory.settings.READ",
)

# Zoho data centers: accounts (OAuth) and API hosts differ per region.
SUPPORTED_DCS = ("com", "in", "eu", "com.au", "jp", "ca", "sa", "com.cn")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    env: str = "dev"
    log_level: str = "INFO"

    # Zoho OAuth client (required)
    zoho_dc: str = "in"
    zoho_org_id: str
    zoho_client_id: str
    zoho_client_secret: SecretStr
    zoho_redirect_uri: str = "http://localhost:8765/oauth/callback"

    # Local state (git-ignored)
    token_file: Path = Path(".tokens/zoho_connector.json")
    budget_file: Path = Path(".tokens/daily_budget.json")

    # Rate limiting. Zoho limits: 100 req/min per org, 1000 req/day (free plan),
    # 5 concurrent (free plan). Defaults leave headroom for other API consumers.
    rate_limit_per_minute: int = 90
    max_concurrency: int = 5
    daily_request_budget: int = 900
    # Pause applied to all calls after a 429 that has no Retry-After header.
    rate_limit_cooldown_seconds: float = 15.0

    # HTTP + retries
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 4
    retry_wait_min: float = 1.0
    retry_wait_max: float = 30.0

    # Tool output limits
    max_page_size: int = 50
    max_scan_pages: int = 5

    @property
    def accounts_url(self) -> str:
        return f"https://accounts.zoho.{self.zoho_dc}"

    @property
    def api_base_url(self) -> str:
        return f"https://www.zohoapis.{self.zoho_dc}/inventory/v1"

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    if settings.zoho_dc not in SUPPORTED_DCS:
        raise ValueError(f"ZOHO_DC must be one of {SUPPORTED_DCS}, got {settings.zoho_dc!r}")
    return settings
