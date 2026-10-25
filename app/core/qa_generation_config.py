"""Server-only answer-generation settings; never expose credentials in API output."""
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnswerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    qa_openai_timeout_seconds: float = Field(default=35, gt=0, le=120)
    qa_openai_max_output_tokens: int = Field(default=1200, ge=256, le=4000)
    qa_repair_attempts: int = Field(default=1, ge=0, le=2)
    qa_record_runs: bool = False  # Enable only after the Phase C migration is applied.


@lru_cache(maxsize=1)
def get_answer_settings() -> AnswerSettings:
    return AnswerSettings()
