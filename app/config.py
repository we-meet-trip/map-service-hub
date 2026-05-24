"""hub-service 환경설정."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    HUB_DATABASE_URL: str
    KMA_SERVICE_KEY: str
    INTERNAL_SERVICE_TOKEN: str = ""

    KMA_POLL_INTERVAL_SEC: float = 1.5
    KMA_RETRY_INTERVAL_SEC: int = 30
    KMA_RETRY_MAX_DURATION_SEC: int = 1200
    KMA_REQUEST_TIMEOUT_SEC: float = 10.0
    KMA_NUMOFROWS: int = 1200
    KMA_RATE_LIMIT_SLEEP_SEC: float = 2.0

    HUB_INTERNAL_TRUSTED_CIDRS: str = (
        "172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"
    )


settings = Settings()
