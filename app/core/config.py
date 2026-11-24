"""Application settings.

All configuration comes from environment variables (or a local `.env` file).
Settings are read once and cached, so the whole app shares one instance.
Unknown variables are ignored, so an older `.env` keeps working.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENVIRONMENTS = {"development", "test", "staging", "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ------------------------------------------------------------------ App
    app_name: str = "LegalMind AI API"
    app_version: str = "0.3.0"
    environment: str = "development"  # development | test | staging | production
    # Avoid reading a generic DEBUG variable set by unrelated tools/shells.
    debug: bool = Field(default=False, validation_alias="LEGALMIND_DEBUG")
    log_level: str = "INFO"
    # "json" emits one JSON object per line for log aggregation.
    log_format: Literal["text", "json"] = "text"

    # Comma separated list, e.g. "http://localhost:3000,https://legalmind.example.com"
    cors_origins: str = "http://localhost:3000"
    # Optional shared secrets for /ask and /search/legal (comma separated).
    # Empty keeps those endpoints open, as before; set it for any public deployment.
    api_keys: SecretStr = SecretStr("")
    # Per-client request ceiling for /ask and /search/legal; 0 disables it.
    rate_limit_per_minute: int = Field(default=60, ge=0, le=100000)
    # Largest accepted request body. Uploads are additionally checked per route.
    max_request_bytes: int = Field(default=110 * 1024 * 1024, ge=1024)
    # Interactive API docs are served outside production unless set explicitly.
    docs_enabled: bool | None = None

    # ------------------------------------------------- Database (Postgres)
    database_url: str = Field(..., description="Postgres connection string")
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout: int = 30  # seconds to wait for a free connection
    db_pool_recycle: int = 1800  # replace connections older than 30 min
    db_connect_timeout: int = 20  # seconds for one connection attempt (TLS + login)
    db_command_timeout: int = Field(default=120, ge=1)  # bound stalled network/query waits
    db_connect_retries: int = 3  # startup attempts before running without DB
    db_connect_retry_delay: float = 2.0  # seconds between startup attempts
    db_ssl: bool = True  # Supabase requires SSL
    # CA certificate file used to verify the database server (recommended in
    # production). Without it the connection is encrypted but not verified.
    db_ssl_root_cert: str | None = None

    # Legacy migration 20260920_02 uses this width while upgrading an empty
    # historical vector table, which migration 20261009_12 later drops.
    # It is not a model choice or active embedding pipeline.
    embedding_dim: int = 384

    # ---------------------------------------------------- Contract uploads
    upload_dir: str = "uploads/contracts"
    upload_max_size_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    upload_max_pages: int = Field(default=10, gt=0)
    # Below this many extracted characters, treat the PDF as a scan with no
    # selectable text layer and reject it (rule: "selectable-text PDF only").
    upload_min_text_chars: int = 20

    # ---------------------------------------- Local legal-corpus administration
    # Deliberately off by default. No login exists yet, so these endpoints must
    # only be enabled while both frontend and backend are bound to loopback.
    legal_ingest_enabled: bool = False
    legal_upload_dir: str = "uploads/legal"
    legal_upload_max_size_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    legal_upload_max_pages: int = Field(default=2000, gt=0)
    legal_ocr_tessdata_dir: str = "uploads/ocr/tessdata"
    legal_ocr_languages: str = "eng"
    legal_ocr_dpi: int = Field(default=200, ge=72, le=600)
    legal_preview_dpi: int = Field(default=100, ge=36, le=150)
    # How long a worker may hold a job before another worker may reclaim it.
    legal_worker_lease_seconds: int = Field(default=900, ge=30, le=86400)

    # ------------------------------------------------- Retrieval (Phase B)
    # Search and /ask serve the active publication only.
    legal_search_enabled: bool = False
    legal_search_candidate_pool: int = Field(default=50, ge=10, le=500)
    # Noise floor for vector candidates. Relevance is judged by fusion,
    # reranking and the answer model, not by this threshold.
    legal_search_min_similarity: float = Field(default=0.30, ge=0, le=1)
    legal_search_rrf_k: int = Field(default=60, ge=1, le=1000)
    legal_search_timeout_seconds: float = Field(default=30, gt=0, le=230)
    # The active publication is held in memory and re-checked on this interval.
    publication_refresh_seconds: float = Field(default=60, ge=0, le=86400)
    # Local cache of the immutable publication snapshot; empty disables it.
    publication_cache_dir: str = ".cache/publications"
    # Load the query encoder and reranker before the first question arrives.
    models_warm_on_startup: bool = True

    # ----------------------------------------------- Question answering (C)
    qa_request_timeout_seconds: float = Field(default=60, gt=0, le=230)
    qa_max_concurrent_requests: int = Field(default=8, ge=1, le=256)
    # A request waits this long for a free slot before it is told to retry.
    qa_queue_timeout_seconds: float = Field(default=10, ge=0, le=120)
    qa_history_max_turns: int = Field(default=6, ge=0, le=20)
    # Ask the model to restate a question that retrieval could not match well
    # (non-English, heavy slang or very garbled wording), then search again.
    qa_rewrite_enabled: bool = True
    qa_rewrite_below_similarity: float = Field(default=0.45, ge=0, le=1)

    # Cross-encoder reranking. "auto" runs it only when the hardware can score
    # the candidates inside qa_rerank_budget_ms; otherwise fused ranks are used.
    qa_rerank_mode: Literal["auto", "on", "off"] = "auto"
    qa_rerank_model_name: str = "BAAI/bge-reranker-v2-m3"
    qa_rerank_budget_ms: int = Field(default=1500, ge=100, le=120000)
    qa_rerank_candidates: int = Field(default=8, ge=2, le=50)
    qa_rerank_max_pair_tokens: int = Field(default=384, ge=64, le=8192)
    qa_rerank_batch_size: int = Field(default=8, ge=1, le=64)
    qa_rerank_device: str = "cpu"
    qa_rerank_use_fp16: bool = False
    qa_rerank_top_k: int = Field(default=6, ge=1, le=20)

    qa_context_max_units: int = Field(default=12, ge=1, le=40)
    qa_context_max_chars: int = Field(default=24000, ge=2000, le=200000)
    qa_context_max_reference_sections: int = Field(default=2, ge=0, le=10)
    # Provisions that cite the best-ranked ones by number (penalty and exception clauses).
    qa_context_max_referring_sections: int = Field(default=2, ge=0, le=10)
    qa_context_max_definition_sections: int = Field(default=1, ge=0, le=5)

    # Answer model. Keys stay in the server environment and are never returned.
    llm_provider: Literal["auto", "gemini", "openai"] = "auto"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-3.5-flash-lite", pattern=r"^[a-z0-9][a-z0-9.-]{0,100}$")
    # Used when the primary model is overloaded or rate limited.
    gemini_fallback_model: str | None = Field(default="gemini-3.1-flash-lite",
                                              pattern=r"^[a-z0-9][a-z0-9.-]{0,100}$")
    # Total time allowed for one model exchange, including retries and hedges.
    qa_gemini_timeout_seconds: float = Field(default=25, gt=0, le=180)
    qa_gemini_max_output_tokens: int = Field(default=1500, ge=256, le=8000)
    qa_gemini_thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    # Total time allowed for one OpenAI exchange, including retries.
    qa_openai_timeout_seconds: float = Field(default=30, gt=0, le=120)
    qa_openai_max_output_tokens: int = Field(default=1500, ge=256, le=8000)
    qa_llm_temperature: float = Field(default=0.2, ge=0, le=2)
    # Failed model calls (timeout, overload, malformed output) that are replaced
    # before the exchange gives up; the time limit above may end it sooner.
    qa_llm_max_retries: int = Field(default=5, ge=0, le=10)
    # While a model call has not answered after this long, another call is
    # started beside it and the first reply wins. 0 disables hedging.
    qa_llm_hedge_seconds: float = Field(default=3.0, ge=0, le=60)
    # One corrective regeneration when an answer cites nothing verifiable.
    qa_repair_attempts: int = Field(default=1, ge=0, le=2)
    # Store question/answer audit rows (needs migrations 20261017_18 and 20261111_19).
    qa_record_runs: bool = False

    # ------------------------------------------------------------ Validators
    @field_validator("database_url")
    @classmethod
    def use_async_driver(cls, value: str) -> str:
        """Accept a normal Postgres URL and switch it to the asyncpg driver."""
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    @field_validator("environment")
    @classmethod
    def known_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ENVIRONMENTS:
            raise ValueError(f"ENVIRONMENT must be one of {sorted(_ENVIRONMENTS)}")
        return normalized

    @field_validator("log_level")
    @classmethod
    def known_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO or DEBUG")
        return normalized

    @field_validator("gemini_fallback_model", "qa_gemini_thinking_level", "db_ssl_root_cert", mode="before")
    @classmethod
    def blank_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    # ------------------------------------------------------------ Properties
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(key.strip() for key in self.api_keys.get_secret_value().split(",") if key.strip())

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def serve_docs(self) -> bool:
        return (not self.is_production) if self.docs_enabled is None else self.docs_enabled

    @property
    def use_gemini(self) -> bool:
        key = self.gemini_api_key.get_secret_value() if self.gemini_api_key else ""
        return self.llm_provider == "gemini" or (self.llm_provider == "auto" and bool(key))

    @property
    def active_model(self) -> str:
        return self.gemini_model if self.use_gemini else self.openai_model

    @property
    def llm_configured(self) -> bool:
        key = self.gemini_api_key if self.use_gemini else self.openai_api_key
        return bool(key and key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
