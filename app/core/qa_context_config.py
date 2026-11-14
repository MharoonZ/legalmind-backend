"""Answer-context budgets; upstream retrieval settings are unchanged."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="QA_CONTEXT_", extra="ignore", frozen=True,
    )
    max_units: int = Field(default=12, ge=1, le=40)
    max_chars: int = Field(default=16000, ge=1000, le=100000)
    max_tokens: int = Field(default=3000, ge=128, le=32000)
    max_reference_depth: int = Field(default=1, ge=0, le=4)
    max_definition_sections: int = Field(default=2, ge=0, le=5)


@lru_cache
def get_context_settings() -> ContextSettings:
    return ContextSettings()
