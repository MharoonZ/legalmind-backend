"""Build the LLM client from settings."""

from __future__ import annotations

from app.core.config import Settings
from app.services.llm.base import LLMClient
from app.services.llm.openai import OpenAIClient


def build_llm_client(settings: Settings) -> LLMClient:
    return OpenAIClient(
        settings.openai_api_key,
        model=settings.openai_model,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        json_mode=settings.llm_json_mode,
        token_param=settings.llm_token_param,
    )
