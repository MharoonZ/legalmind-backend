"""OpenAI chat client (Chat Completions endpoint), plain httpx, no SDK.

OpenAI models differ in small ways (older ones want `max_tokens`, reasoning
models refuse a custom temperature), so the client adapts instead of failing:

* JSON mode: starts with strict `json_schema`; if the model rejects it,
  steps down to `json_object`, then to plain text (the answer is still parsed
  as JSON and validated afterwards).
* Token limit: switches `max_completion_tokens` <-> `max_tokens` on demand.
* Temperature: dropped if the model refuses a custom value.
* Reasoning models: `<think>...</think>` blocks are stripped before parsing.

Each adaptation is logged once and remembered for the life of the process.
Retries with backoff cover timeouts, 429 and 5xx only. A bad key, unknown model
or "insufficient_quota" cannot be fixed by retrying and raises straight away.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.errors import LLMError
from app.services.llm.base import LLMAnswer, parse_llm_json

logger = logging.getLogger(__name__)

STRICT_SCHEMA_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "legalmind_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "supported": {"type": "boolean"},
                "answer": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "required": ["evidence_id", "quote"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["supported", "answer", "citations"],
            "additionalProperties": False,
        },
    },
}
# Kept for backwards compatibility with earlier imports.
RESPONSE_FORMAT = STRICT_SCHEMA_FORMAT

_JSON_MODE_STEPS = ["json_schema", "json_object", "none"]
_JSON_ERROR_HINTS = ("response_format", "json_schema", "json_object", "structured", "schema")
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
# Reasoning models reject a custom temperature.
_FIXED_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")


class OpenAIClient:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.1,
        max_output_tokens: int = 2048,
        json_mode: str = "json_schema",
        token_param: str = "max_completion_tokens",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, max_retries)
        self.max_output_tokens = max_output_tokens
        self._client = client or httpx.AsyncClient(timeout=timeout)

        self._json_mode = json_mode if json_mode in _JSON_MODE_STEPS else "json_schema"
        self._token_param = token_param
        self._temperature: float | None = temperature if temperature >= 0 else None
        if model.lower().startswith(_FIXED_TEMPERATURE_PREFIXES):
            self._temperature = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------- request
    def _build_payload(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            self._token_param: self.max_output_tokens,
        }
        if self._json_mode == "json_schema":
            payload["response_format"] = STRICT_SCHEMA_FORMAT
        elif self._json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        return payload

    def _adapt_to_rejection(self, body: str) -> bool:
        """Change one setting the provider objected to. True if something changed."""
        lowered = body.lower()

        if self._temperature is not None and "temperature" in lowered:
            logger.warning("%s does not accept a custom temperature; omitting it", self.model)
            self._temperature = None
            return True

        if "max_completion_tokens" in lowered and self._token_param == "max_completion_tokens":
            logger.warning("Provider wants max_tokens instead of max_completion_tokens")
            self._token_param = "max_tokens"
            return True
        if "max_tokens" in lowered and self._token_param == "max_tokens":
            logger.warning("Provider wants max_completion_tokens instead of max_tokens")
            self._token_param = "max_completion_tokens"
            return True

        if self._json_mode != "none" and any(hint in lowered for hint in _JSON_ERROR_HINTS):
            next_mode = _JSON_MODE_STEPS[_JSON_MODE_STEPS.index(self._json_mode) + 1]
            logger.warning(
                "Provider rejected JSON mode '%s'; falling back to '%s'",
                self._json_mode,
                next_mode,
            )
            self._json_mode = next_mode
            return True
        return False

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("The model returned no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise LLMError(f"The model refused the request: {str(message['refusal'])[:200]}")
        text = (message.get("content") or "").strip()
        if not text:
            raise LLMError(f"The model returned empty text (finish_reason={choice.get('finish_reason')})")
        if choice.get("finish_reason") == "length":
            raise LLMError("The answer was cut off; raise LLM_MAX_OUTPUT_TOKENS")
        return text

    async def generate(self, system_prompt: str, user_prompt: str) -> LLMAnswer:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        last_error: Exception | None = None
        adaptations = 0
        attempt = 0

        while attempt <= self.max_retries:
            payload = self._build_payload(system_prompt, user_prompt)
            try:
                response = await self._client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "LLM request failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    exc.__class__.__name__,
                )
            else:
                if response.status_code == 200:
                    return parse_llm_json(self._extract_text(response.json()))

                body = response.text[:500]
                out_of_credit = "insufficient_quota" in body
                if response.status_code == 400 and adaptations < 4 and self._adapt_to_rejection(body):
                    adaptations += 1
                    continue  # retry immediately with the adjusted request
                if response.status_code not in _RETRYABLE_STATUS or out_of_credit:
                    hint = " (no credit left: add billing with your provider)" if out_of_credit else ""
                    raise LLMError(
                        f"LLM rejected the request ({response.status_code}){hint}: {body[:300]}"
                    )
                last_error = LLMError(f"LLM HTTP {response.status_code}")
                logger.warning(
                    "LLM returned %d (attempt %d/%d)",
                    response.status_code,
                    attempt + 1,
                    self.max_retries + 1,
                )

            attempt += 1
            if attempt <= self.max_retries:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        raise LLMError(f"LLM unavailable after {self.max_retries + 1} attempts") from last_error

    async def aclose(self) -> None:
        await self._client.aclose()
