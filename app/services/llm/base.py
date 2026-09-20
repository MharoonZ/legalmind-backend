"""The contract every LLM client must satisfy.

Keeping this narrow means `AskService` never imports OpenAI directly, so the
tests can swap in a fake and a future model swap touches one file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.errors import LLMError

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Reasoning models (DeepSeek-R1, Qwen, ...) may prefix their answer with this.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass(slots=True)
class LLMAnswer:
    """The raw, still-untrusted answer from the model."""

    supported: bool
    answer: str
    citations: list[dict] = field(default_factory=list)


@runtime_checkable
class LLMClient(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    async def generate(self, system_prompt: str, user_prompt: str) -> LLMAnswer: ...

    async def aclose(self) -> None: ...


def parse_llm_json(raw_text: str) -> LLMAnswer:
    """Parse the model's JSON, tolerating markdown fences and stray text."""
    text = _THINK_RE.sub("", raw_text or "")
    text = _FENCE_RE.sub("", text.strip()).strip()
    if not text:
        raise LLMError("Model returned an empty response")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model response was not JSON") from None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("Model response was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise LLMError("Model response was not a JSON object")

    answer = str(payload.get("answer", "")).strip()
    if not answer:
        raise LLMError("Model response had no answer text")

    citations: list[Any] = payload.get("citations") or []
    if not isinstance(citations, list):
        citations = []

    return LLMAnswer(
        supported=bool(payload.get("supported", False)),
        answer=answer,
        citations=[c for c in citations if isinstance(c, dict)],
    )
