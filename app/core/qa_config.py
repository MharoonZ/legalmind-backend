"""Settings owned by the answer pipeline; upstream retrieval settings are unchanged."""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RerankerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="QA_RERANK_", extra="ignore", frozen=True,
    )
    model_name: Literal["BAAI/bge-reranker-v2-m3"] = "BAAI/bge-reranker-v2-m3"
    top_k: int = Field(default=5, ge=1, le=10)
    device: str = "cpu"
    use_fp16: bool = False
    batch_size: int = Field(default=4, ge=1, le=32)
    batch_token_budget: int = Field(default=2048, ge=256, le=65536)
    max_pair_tokens: int = Field(default=8192, ge=32, le=8192)
    warm_on_startup: bool = True


@lru_cache
def get_reranker_settings() -> RerankerSettings:
    return RerankerSettings()
