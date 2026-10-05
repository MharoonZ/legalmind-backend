"""Application settings.

All configuration comes from environment variables (or a local `.env` file).
Settings are read once and cached, so the whole app shares one instance.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
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
    db_connect_retries: int = 3  # startup attempts before running without DB
    db_connect_retry_delay: float = 2.0  # seconds between startup attempts
    db_ssl: bool = True  # Supabase requires SSL

    # ------------------------------------------------------------ Embeddings
    # all-MiniLM-L6-v2 is small, free, runs on CPU and produces 384 dimensions,
    # which pgvector can index with HNSW (its index limit is 2000 dimensions).
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384
    embedding_device: str = "cpu"  # "cuda" if a GPU is available
    embedding_batch_size: int = 32
    # Load the model when the API starts instead of on the first question.
    embedding_warmup: bool = True
    # Turn off to run keyword-only retrieval (useful before the model is downloaded).
    embeddings_enabled: bool = True

    # ------------------------------------------------------------- Retrieval
    retrieval_keyword_limit: int = 20  # candidates from full-text search
    retrieval_vector_limit: int = 20  # candidates from vector search
    retrieval_top_k: int = 5  # passages kept after the RRF merge
    retrieval_rrf_k: int = 60  # RRF damping constant
    # Evidence gate: below both thresholds the API answers "unsupported"
    # instead of asking the LLM to guess.
    retrieval_min_keyword_rank: float = 0.02
    retrieval_min_vector_similarity: float = 0.30
    # Only text from sources in one of these review states may be quoted.
    # Tighten to "approved" before any real user sees the answers.
    retrieval_review_statuses: str = "needs_review,approved"
    # Sections the corpus covers. A question naming a section outside this
    # range gets a direct-lookup miss instead of a confidently wrong section.
    corpus_section_min: int = 1
    corpus_section_max: int = 30

    # ---------------------------------------------------------------- OpenAI
    # OPENAI_API_KEY / OPENAI_MODEL are the names to use. LLM_API_KEY and
    # LLM_MODEL are accepted too, so an older .env keeps working.
    openai_api_key: str = Field(
        default="", validation_alias=AliasChoices("OPENAI_API_KEY", "LLM_API_KEY")
    )
    openai_model: str = Field(
        default="gpt-4o-mini", validation_alias=AliasChoices("OPENAI_MODEL", "LLM_MODEL")
    )
    openai_base_url: str = "https://api.openai.com/v1"
    # How the JSON answer is requested. The client steps down automatically
    # (json_schema -> json_object -> none) if a model rejects the stricter mode.
    llm_json_mode: Literal["json_schema", "json_object", "none"] = "json_schema"
    # The client also switches this on its own if the API asks for the other one.
    llm_token_param: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    llm_timeout: float = 30.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.1  # negative = do not send (some models reject it)
    llm_max_output_tokens: int = 2048

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

    # ------------------------------------------------------------ Validators
    @field_validator("database_url")
    @classmethod
    def use_async_driver(cls, value: str) -> str:
        """Accept a normal Postgres URL and switch it to the asyncpg driver."""
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    @field_validator("retrieval_review_statuses")
    @classmethod
    def known_review_statuses(cls, value: str) -> str:
        allowed = {"needs_review", "approved", "rejected"}
        chosen = {item.strip() for item in value.split(",") if item.strip()}
        if not chosen:
            raise ValueError("retrieval_review_statuses must not be empty")
        unknown = chosen - allowed
        if unknown:
            raise ValueError(f"Unknown review status: {', '.join(sorted(unknown))}")
        if "rejected" in chosen:
            raise ValueError("Rejected text must never be quoted back to a user")
        return value

    # ------------------------------------------------------------ Properties
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def review_statuses_list(self) -> list[str]:
        return [s.strip() for s in self.retrieval_review_statuses.split(",") if s.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def llm_provider(self) -> str:
        return "openai"

    @property
    def llm_model(self) -> str:
        return self.openai_model

    @property
    def llm_base_url(self) -> str:
        return self.openai_base_url.rstrip("/")

    @property
    def llm_api_key_name(self) -> str:
        return "OPENAI_API_KEY"

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
