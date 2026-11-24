"""Structured-output calls to the answer model (Gemini or OpenAI).

One shared HTTP client is reused across requests. Timeouts, overload (429/5xx)
and malformed output are retried with backoff, optionally on a fallback model,
before the caller is told the provider is unavailable. Provider credentials
and prompt contents are never logged or returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from time import perf_counter

import httpx

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})
_OVERLOADED = frozenset({429, 503})
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


class GenerationUnavailable(RuntimeError):
    """Credentials, network or provider failure; says nothing about the law."""


class GenerationInvalid(RuntimeError):
    """The provider answered, but not with usable structured output."""


def _parse_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        match = _JSON_OBJECT.search(text)       # tolerate a code fence or stray prose
        if not match:
            raise GenerationInvalid("The answer model did not return JSON.") from None
        try:
            value = json.loads(match[0])
        except ValueError:
            raise GenerationInvalid("The answer model returned malformed JSON.") from None
    if not isinstance(value, dict):
        raise GenerationInvalid("The answer model returned JSON that is not an object.")
    return value


class AnswerGenerator:
    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None, openai_client=None,
                 sleep=asyncio.sleep):
        self.settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._openai = openai_client
        self._sleep = sleep
        self._fallback_missing = False

    @property
    def provider(self) -> str:
        return "gemini" if self.settings.use_gemini else "openai"

    @property
    def model(self) -> str:
        return self.settings.active_model

    @property
    def configured(self) -> bool:
        return bool(self.settings.llm_configured or (not self.settings.use_gemini and self._openai is not None))

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        client, self._openai = self._openai, None
        if client is not None and hasattr(client, "close"):
            await client.close()

    async def generate_json(self, system: str, user: str, schema: dict, *,
                            max_output_tokens: int | None = None, purpose: str = "answer") -> dict:
        """Return one JSON object that follows ``schema``."""
        started = perf_counter()
        try:
            if self.settings.use_gemini:
                try:
                    result, calls = await self._race(system, user, schema, max_output_tokens)
                except (GenerationUnavailable, GenerationInvalid):
                    if not self._openai_ready():
                        raise
                    # Gemini is down for this request: use the configured OpenAI model.
                    logger.warning("llm_failover purpose=%s from=gemini to=openai", purpose)
                    result, calls = await self._openai_with_retries(system, user, schema, max_output_tokens)
            else:
                result, calls = await self._openai_with_retries(system, user, schema, max_output_tokens)
        except (GenerationUnavailable, GenerationInvalid) as exc:
            logger.warning("llm_call_failed purpose=%s provider=%s error=%s elapsed_ms=%.0f", purpose,
                           self.provider, type(exc).__name__, (perf_counter() - started) * 1000)
            raise
        logger.info("llm_call purpose=%s provider=%s calls=%d elapsed_ms=%.0f", purpose, self.provider,
                    calls, (perf_counter() - started) * 1000)
        return result

    def _gemini_models(self) -> list[str]:
        primary, fallback = self.settings.gemini_model, self.settings.gemini_fallback_model
        if not fallback or fallback == primary or self._fallback_missing:
            return [primary]
        return [primary, fallback]

    async def _race(self, system, user, schema, max_output_tokens) -> tuple[dict, int]:
        """Ask the primary model; bring in the fallback when it is slow or failing.

        A call that fails is replaced at once by the other model. A call that
        has not answered within ``qa_llm_hedge_seconds`` gets a second call
        racing beside it. The first valid reply wins, everything else is
        cancelled, and the whole exchange is bounded by one deadline, so a
        stalled or overloaded model cannot hold a question hostage.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.qa_gemini_timeout_seconds
        budget = 2 + self.settings.qa_llm_max_retries
        hedge = self.settings.qa_llm_hedge_seconds
        pending: set[asyncio.Task] = set()
        launched = 0
        failure: Exception = GenerationUnavailable("The answer model did not respond in time.")

        def launch() -> None:
            nonlocal launched
            models = self._gemini_models()
            model = models[launched % len(models)]
            launched += 1
            pending.add(asyncio.create_task(self._gemini(
                system, user, schema, max_output_tokens, model, max(1.0, deadline - loop.time()))))

        launch()
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise failure
                can_hedge = hedge > 0 and len(pending) == 1 and launched < budget \
                    and len(self._gemini_models()) > 1
                done, pending = await asyncio.wait(
                    pending, timeout=min(hedge, remaining) if can_hedge else remaining,
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    if can_hedge and deadline - loop.time() > 0:
                        launch()                # slow: race the other model
                    continue
                for task in done:
                    try:
                        return task.result(), launched
                    except _Retryable as exc:
                        failure = exc.error
                        if exc.missing_model:
                            self._fallback_missing = True
                if not pending:
                    if launched >= budget:
                        raise failure
                    await self._sleep(min(max(0.0, deadline - loop.time()), 0.25 + random.uniform(0, 0.15)))
                    launch()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _openai_ready(self) -> bool:
        key = self.settings.openai_api_key
        return self._openai is not None or bool(key and key.get_secret_value())

    async def _openai_with_retries(self, system, user, schema, max_output_tokens) -> tuple[dict, int]:
        attempts = 1 + self.settings.qa_llm_max_retries
        failure: Exception = GenerationUnavailable("The answer model is not configured.")
        for attempt in range(attempts):
            try:
                return await self._openai_call(system, user, schema, max_output_tokens), attempt + 1
            except _Retryable as exc:
                failure = exc.error
                if attempt + 1 < attempts:
                    await self._sleep(min(4.0, 0.4 * 2 ** attempt) + random.uniform(0, 0.2))
        raise failure

    # --------------------------------------------------------------- Gemini
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            limits = httpx.Limits(max_connections=32, max_keepalive_connections=8)
            self._http = httpx.AsyncClient(transport=self._transport, limits=limits)
        return self._http

    async def _gemini(self, system, user, schema, max_output_tokens, model: str, timeout: float) -> dict:
        key = self.settings.gemini_api_key.get_secret_value() if self.settings.gemini_api_key else ""
        if not key:
            raise GenerationUnavailable("The server's GEMINI_API_KEY is not configured.")
        config = {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": max_output_tokens or self.settings.qa_gemini_max_output_tokens,
            "temperature": self.settings.qa_llm_temperature,
        }
        if self.settings.qa_gemini_thinking_level:
            config["thinkingConfig"] = {"thinkingLevel": self.settings.qa_gemini_thinking_level.upper()}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        try:
            response = await self._client().post(
                _GEMINI_URL.format(model=model), headers={"x-goog-api-key": key}, json=payload,
                timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.info("llm_attempt_failed model=%s error=%s", model, type(exc).__name__)
            raise _Retryable(GenerationUnavailable(
                "The answer model timed out or could not be reached."), overloaded=True) from None
        if response.status_code in _TRANSIENT:
            logger.info("llm_attempt_failed model=%s status=%d", model, response.status_code)
            raise _Retryable(GenerationUnavailable(
                f"The answer model is temporarily unavailable (HTTP {response.status_code})."),
                overloaded=response.status_code in _OVERLOADED)
        if response.status_code in {401, 403}:
            raise GenerationUnavailable("The answer model rejected the server credential.")
        if response.status_code == 404 and model != self.settings.gemini_model:
            # The fallback model is not offered to this key: stop using it.
            raise _Retryable(GenerationUnavailable("The fallback answer model is not available."),
                             missing_model=True)
        if response.status_code != 200:
            raise GenerationUnavailable(f"The answer model returned HTTP {response.status_code}.")
        try:
            data = response.json()
            candidate = (data.get("candidates") or [{}])[0]
            if data.get("promptFeedback", {}).get("blockReason"):
                raise GenerationInvalid("The answer model declined this request.")
            reason = candidate.get("finishReason")
            parts = (candidate.get("content") or {}).get("parts") or []
            text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        except (ValueError, AttributeError, TypeError):
            raise _Retryable(GenerationInvalid("The answer model returned an unreadable response.")) from None
        usage = data.get("usageMetadata") or {}
        logger.info("llm_usage model=%s input_tokens=%s output_tokens=%s thinking_tokens=%s finish=%s",
                    model, usage.get("promptTokenCount"), usage.get("candidatesTokenCount"),
                    usage.get("thoughtsTokenCount"), reason)
        if reason not in {"STOP", None} or not text.strip():
            raise _Retryable(GenerationInvalid("The answer model did not finish its response."))
        try:
            return _parse_object(text)
        except GenerationInvalid as exc:
            raise _Retryable(exc) from None

    # --------------------------------------------------------------- OpenAI
    def _openai_client(self):
        if self._openai is None:
            key = self.settings.openai_api_key.get_secret_value() if self.settings.openai_api_key else ""
            if not key:
                raise GenerationUnavailable("The server's OPENAI_API_KEY is not configured.")
            from openai import AsyncOpenAI
            self._openai = AsyncOpenAI(api_key=key, timeout=self.settings.qa_openai_timeout_seconds,
                                       max_retries=0)
        return self._openai

    async def _openai_call(self, system, user, schema, max_output_tokens) -> dict:
        client = self._openai_client()
        try:
            response = await client.responses.create(
                model=self.settings.openai_model,
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                text={"format": {"type": "json_schema", "name": "legalmind_response",
                                 "schema": schema, "strict": True}},
                max_output_tokens=max_output_tokens or self.settings.qa_openai_max_output_tokens,
                temperature=self.settings.qa_llm_temperature,
                store=False,
            )
        except Exception as exc:
            # Never include exception text: SDK errors may contain request details.
            name = type(exc).__name__
            if name in {"AuthenticationError", "PermissionDeniedError"}:
                raise GenerationUnavailable("The answer model rejected the server credential.") from None
            if name in {"BadRequestError", "NotFoundError", "UnprocessableEntityError"}:
                raise GenerationUnavailable("The answer model rejected the request.") from None
            raise _Retryable(GenerationUnavailable("The answer model failed or could not be reached."),
                             overloaded=name == "RateLimitError") from None
        if getattr(response, "status", "completed") != "completed":
            raise _Retryable(GenerationInvalid("The answer model did not finish its response."))
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise _Retryable(GenerationInvalid("The answer model refused or returned no content."))
        try:
            return _parse_object(text)
        except GenerationInvalid as exc:
            raise _Retryable(exc) from None


class _Retryable(Exception):
    def __init__(self, error: Exception, *, overloaded: bool = False, missing_model: bool = False):
        self.error = error
        self.overloaded = overloaded
        self.missing_model = missing_model
        super().__init__(str(error))
