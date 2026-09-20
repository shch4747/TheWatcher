"""Settings loaded from the environment. One place, per ADR-0005."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gowa_base_url: str = "http://localhost:3000"
    gowa_basic_auth: str | None = None
    gowa_device_id: str | None = None
    gowa_webhook_secret: str = "dev-secret"
    wa_send_min_gap_s: float = 4.0

    database_url: str = "sqlite+aiosqlite:///./watcher.db"
    vault_root: str = "./vault"


settings = Settings()
