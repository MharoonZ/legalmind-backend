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
        populate_by_name=True,
    )

    # ------------------------------------------------------------------ App
    app_name: str = "LegalMind AI API"
    app_version: str = "0.2.0"
    environment: str = "development"  # development | staging | production
    # Avoid reading a generic DEBUG variable set by unrelated tools/shells.
    debug: bool = Field(default=False, validation_alias="LEGALMIND_DEBUG")
    log_level: str = "INFO"

    # Comma separated list, e.g. "http://localhost:3000,https://legalmind.example.com"
    cors_origins: str = "http://localhost:3000"

    # ------------------------------------------------- Database (Postgres)
    database_url: str = Field(..., description="Postgres connection string")
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: int = 30  # seconds to wait for a free connection
    db_pool_recycle: int = 1800  # replace connections older than 30 min
    db_connect_timeout: int = 20  # seconds for one connection attempt (TLS + login)
    db_command_timeout: int = Field(default=120, ge=1)  # bound stalled network/query waits
    db_connect_retries: int = 3  # startup attempts before running without DB
    db_connect_retry_delay: float = 2.0  # seconds between startup attempts
    db_ssl: bool = True  # Supabase requires SSL

    # Legacy migration 20260920_02 uses this width while upgrading an empty
    # historical vector table, which migration 20261009_12 later drops.
    # It is not a model choice or active embedding pipeline.
    embedding_dim: int = 384

    # ---------------------------------------------------- Contract uploads
    # Sprint 2 scope (Abdullah, Week 1): upload + validate + save only.
    # Local disk for now (local/team-only demo); swap for object storage
    # later without changing the /contracts response shape.
    upload_dir: str = "uploads/contracts"
    upload_max_size_bytes: int = 10 * 1024 * 1024  # 10 MB, per Sprint 2 scope
    upload_max_pages: int = 10  # per Sprint 2 scope
    # Below this many extracted characters, treat the PDF as a scan with no
    # selectable text layer and reject it (rule: "selectable-text PDF only").
    # This is a presence check only, not extraction - see document_upload.py.
    upload_min_text_chars: int = 20

    # ---------------------------------------- Local legal-corpus administration
    # Deliberately off by default. No login exists yet, so this endpoint must
    # only be enabled while both frontend and backend are bound to loopback.
    legal_ingest_enabled: bool = False
    legal_upload_dir: str = "uploads/legal"
    legal_upload_max_size_bytes: int = 100 * 1024 * 1024
    legal_upload_max_pages: int = 2000
    legal_ocr_tessdata_dir: str = "uploads/ocr/tessdata"
    legal_ocr_languages: str = "eng"
    legal_ocr_dpi: int = 200

    # Compatibility for the earlier shimza extraction CLI and read-only inspector.
    # The current workers and their configuration above are unchanged.
    legal_worker_lease_seconds: int = Field(default=120, ge=15, le=3600)
    legal_worker_max_attempts: int = Field(default=3, ge=1, le=10)
    legal_page_timeout: int = Field(default=90, ge=5, le=600)
    legal_ocr_enabled: bool = True
    legal_tesseract_path: str = "tesseract"
    legal_tessdata_dir: str = ""
    legal_native_min_chars: int = Field(default=40, ge=1, le=1000)
    legal_preview_dpi: int = Field(default=100, ge=36, le=150)

    # Search exposes evidence candidates from the active publication only.
    # The encoder is loaded lazily; existing API startup remains lightweight.
    legal_search_enabled: bool = False
    legal_search_candidate_pool: int = Field(default=50, ge=10, le=100)
    legal_search_min_similarity: float = Field(default=0.48, ge=0, le=1)
    legal_search_rrf_k: int = Field(default=60, ge=1, le=1000)
    legal_search_hnsw_ef_search: int = Field(default=200, ge=40, le=1000)

    # ------------------------------------------------------------ Validators
    @field_validator("database_url")
    @classmethod
    def use_async_driver(cls, value: str) -> str:
        """Accept a normal Postgres URL and switch it to the asyncpg driver."""
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    # ------------------------------------------------------------ Properties
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
