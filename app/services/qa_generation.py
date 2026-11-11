"""Step 15: request structured output; keep provider failures distinct from evidence gaps."""
from __future__ import annotations

from json import JSONDecodeError

import httpx
from pydantic import ValidationError

from app.core.qa_generation_config import AnswerSettings, get_answer_settings
from app.schemas.qa_generation import AnswerDraft
from app.services.qa_prompt import PromptPacket


class GenerationUnavailable(RuntimeError):
    """Credentials or provider unavailable; not a judgment on the law."""


class GenerationInvalid(RuntimeError):
    """The provider returned an incomplete, refused, or invalid structured draft."""


class AnswerGenerator:
    def __init__(self, settings: AnswerSettings | None = None, *, client=None):
        self.settings = settings or get_answer_settings()
        self._client = client

    def _get_client(self):
        if self._client is None:
            if self.settings.openai_api_key is None or not self.settings.openai_api_key.get_secret_value():
                raise GenerationUnavailable("The server's OPENAI_API_KEY is not configured.")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key.get_secret_value(),
                timeout=self.settings.qa_openai_timeout_seconds,
                max_retries=0,
            )
        return self._client

    async def _generate_gemini(self, packet: PromptPacket, user: str) -> AnswerDraft:
        if not self.settings.gemini_api_key or not self.settings.gemini_api_key.get_secret_value():
            raise GenerationUnavailable("The server's GEMINI_API_KEY is not configured.")
        key = self.settings.gemini_api_key.get_secret_value()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}:generateContent?key={key}"

        schema = {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["answered", "partial", "clarification_needed", "insufficient_evidence"],
                },
                "answer": {"type": "string"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "quote": {"type": "string"},
                            "claim_text": {"type": "string"},
                        },
                        "required": ["evidence_id", "quote", "claim_text"],
                    },
                },
                "missing_support": {"type": "string"},
                "clarification_question": {"type": "string", "nullable": True},
            },
            "required": ["outcome", "answer", "claims", "missing_support"],
        }

        payload = {
            "system_instruction": {"parts": [{"text": packet.system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "thinkingConfig": {
                    "thinkingBudget": 0
                },
                "maxOutputTokens": self.settings.qa_openai_max_output_tokens,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                r = await http_client.post(url, json=payload)
        except Exception as exc:
            raise GenerationUnavailable("The Gemini answer provider timed out or could not be reached.") from exc

        if r.status_code == 429:
            raise GenerationUnavailable("The Gemini answer provider's rate limit was reached.")
        if r.status_code in {401, 403}:
            raise GenerationUnavailable("The Gemini answer provider rejected the server credential.")
        if r.status_code >= 500:
            raise GenerationUnavailable("The Gemini answer provider failed to process the request.")
        if r.status_code != 200:
            raise GenerationUnavailable(f"The Gemini answer provider returned error status {r.status_code}.")

        try:
            data = r.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise GenerationInvalid("The Gemini answer provider returned no candidates.")
            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return AnswerDraft.model_validate_json(raw_text)
        except (ValidationError, JSONDecodeError) as exc:
            raise GenerationInvalid("The Gemini answer provider returned an invalid structured answer.") from exc
        except Exception as exc:
            raise GenerationInvalid("The Gemini answer provider returned an unparseable response.") from exc

    async def generate(self, packet: PromptPacket, *, repair_feedback: str | None = None) -> AnswerDraft:
        user = packet.user
        if repair_feedback:
            user += (
                "\nVerification feedback (server data): "
                + repair_feedback
                + "\nCorrect the draft using only the supplied evidence. If that is impossible, return insufficient_evidence."
            )

        use_gemini = (
            self.settings.llm_provider == "gemini"
            or (self.settings.llm_provider == "auto" and self.settings.gemini_api_key and self.settings.gemini_api_key.get_secret_value())
        )

        if use_gemini:
            return await self._generate_gemini(packet, user)

        client = self._get_client()
        try:
            response = await client.responses.parse(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": packet.system},
                    {"role": "user", "content": user},
                ],
                text_format=AnswerDraft,
                max_output_tokens=self.settings.qa_openai_max_output_tokens,
                store=False,
            )
        except (ValidationError, JSONDecodeError) as exc:
            # The SDK parses model JSON before it returns a response. A schema
            # failure here is an invalid draft, not a provider connection error.
            raise GenerationInvalid("The answer provider returned an invalid structured answer.") from exc
        except Exception as exc:
            # Never include exception text: SDK errors may contain request details.
            name = type(exc).__name__
            if name in {"APITimeoutError", "APIConnectionError"}:
                raise GenerationUnavailable("The answer provider timed out or could not be reached.") from exc
            if name == "RateLimitError":
                raise GenerationUnavailable("The answer provider's rate limit was reached.") from exc
            if name in {"AuthenticationError", "PermissionDeniedError"}:
                raise GenerationUnavailable("The answer provider rejected the server credential.") from exc
            if name in {"APIStatusError", "InternalServerError", "BadRequestError", "OpenAIError"}:
                raise GenerationUnavailable("The answer provider failed to process the request.") from exc
            raise GenerationUnavailable("The answer provider failed unexpectedly.") from exc
        if getattr(response, "status", None) != "completed":
            raise GenerationInvalid("The answer provider returned an incomplete response.")
        if any(
            getattr(part, "type", None) == "refusal"
            for item in (getattr(response, "output", None) or ())
            if getattr(item, "type", None) == "message"
            for part in (getattr(item, "content", None) or ())
        ):
            raise GenerationInvalid("The answer provider refused to return a structured answer.")
        draft = getattr(response, "output_parsed", None)
        if draft is None:
            raise GenerationInvalid("The answer provider refused or returned no structured answer.")
        try:
            # Pydantic can otherwise trust an existing model instance without
            # checking changes made by model_copy/model_construct or adapters.
            data = draft.model_dump(warnings=False) if isinstance(draft, AnswerDraft) else draft
            return AnswerDraft.model_validate(data, strict=True)
        except Exception as exc:
            raise GenerationInvalid("The answer provider returned an invalid structured answer.") from exc
