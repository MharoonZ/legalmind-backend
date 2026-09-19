"""Application settings.

All configuration comes from environment variables (or a local `.env` file).
Settings are read once and cached, so the whole app shares one instance.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "LegalMind AI API"
    app_version: str = "0.1.0"
    environment: str = "development"  # development | staging | production
    debug: bool = False
    log_level: str = "INFO"

    # Comma separated list, e.g. "http://localhost:3000,https://legalmind.example.com"
    cors_origins: str = "http://localhost:3000"

    # --- Database (Postgres / Supabase) ---
    database_url: str = Field(..., description="Postgres connection string")
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: int = 30  # seconds to wait for a free connection
    db_pool_recycle: int = 1800  # replace connections older than 30 min
    db_connect_timeout: int = 5  # seconds for one connection attempt
    db_connect_retries: int = 3  # startup attempts before running without DB
    db_connect_retry_delay: float = 2.0  # seconds between startup attempts
    db_ssl: bool = True  # Supabase requires SSL

    @field_validator("database_url")
    @classmethod
    def use_async_driver(cls, value: str) -> str:
        """Accept a normal Postgres URL and switch it to the asyncpg driver."""
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
