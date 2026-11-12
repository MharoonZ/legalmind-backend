"""Server-only answer-generation settings; never expose credentials in API output."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnswerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-3.1-flash-lite", pattern=r"^[a-z0-9][a-z0-9.-]{0,100}$")
    llm_provider: Literal["auto", "gemini", "openai"] = "auto"
    qa_gemini_timeout_seconds: float = Field(default=35, gt=0, le=120)
    qa_gemini_max_output_tokens: int = Field(default=1200, ge=256, le=4000)
    qa_gemini_thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    qa_openai_timeout_seconds: float = Field(default=35, gt=0, le=120)
    qa_openai_max_output_tokens: int = Field(default=1200, ge=256, le=4000)
    qa_repair_attempts: int = Field(default=1, ge=0, le=2)
    qa_record_runs: bool = False  # Enable only after the Phase C migration is applied.

    @property
    def use_gemini(self) -> bool:
        return self.llm_provider == "gemini" or (
            self.llm_provider == "auto" and bool(self.gemini_api_key))

    @property
    def active_model(self) -> str:
        return self.gemini_model if self.use_gemini else self.openai_model


@lru_cache(maxsize=1)
def get_answer_settings() -> AnswerSettings:
    return AnswerSettings()
