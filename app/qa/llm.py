"""Structured-output calls to the answer model (Gemini or OpenAI).

One shared HTTP client is reused across requests. A model exchange is bounded
by one deadline. Inside it, a call that fails (timeout, 429/5xx, malformed
output) is replaced after a short growing pause, a call that is slow gets
another call racing beside it, and a model that has just reported overload is
avoided for a while in favour of the fallback model. The first valid reply
wins. Provider credentials and prompt contents are never logged or returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from collections import Counter
from time import perf_counter

import httpx

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})
_OVERLOADED = frozenset({429, 503})
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_RETRY_DELAY = re.compile(r"(\d+(?:\.\d+)?)s")
_MAX_PARALLEL = 3            # calls racing for one answer
_COOLDOWN_SECONDS = 20.0     # how long an overloaded model is avoided when another one is available
_MAX_COOLDOWN_SECONDS = 120.0
_MAX_PAUSE_SECONDS = 3.0     # longest wait before a failed call is replaced


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


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the provider asked callers to wait (Retry-After header or RetryInfo detail)."""
    header = response.headers.get("retry-after", "")
    if header.strip().replace(".", "", 1).isdigit():
        return float(header)
    try:
        details = response.json()["error"]["details"]
    except (ValueError, KeyError, TypeError):
        return None
    for detail in details if isinstance(details, list) else ():
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        if isinstance(delay, str) and (match := _RETRY_DELAY.fullmatch(delay.strip())):
            return float(match[1])
    return None


def _pause(failures: int) -> float:
    """Wait before replacing a failed call: 0.3 s, doubling, with jitter."""
    return min(_MAX_PAUSE_SECONDS, 0.3 * 2 ** max(0, failures - 1)) + random.uniform(0, 0.15)


class AnswerGenerator:
    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None, openai_client=None,
                 sleep=asyncio.sleep):
        self.settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._openai = openai_client
        self._sleep = sleep
        self._fallback_missing = False
        # model -> (loop time until which it is avoided, whether the provider stated that wait).
        # A stated wait (rate limit) is obeyed; an assumed one (overload) only orders the models.
        self._avoid: dict[str, tuple[float, bool]] = {}

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

    def _rate_limited_until(self, now: float) -> float:
        """0 when some model may be called now, else when the first rate limit ends."""
        waits = [until for model in self._gemini_models()
                 for until, stated in (self._avoid.get(model, (0.0, False)),) if stated and until > now]
        return min(waits) if len(waits) == len(self._gemini_models()) else 0.0

    def _next_model(self, now: float, in_flight: Counter) -> str:
        """The model for the next call: one that is not being avoided, with the fewest calls in flight."""
        models = self._gemini_models()
        avoided = {model: self._avoid.get(model, (0.0, False)) for model in models}
        healthy = [model for model in models if avoided[model][0] <= now]
        if healthy:
            return min(healthy, key=lambda model: (in_flight[model], models.index(model)))
        # Every model reported trouble: skip rate-limited ones, then take the
        # one that reported overload longest ago.
        open_models = [model for model in models if not avoided[model][1]] or models
        return min(open_models, key=lambda model: (in_flight[model], avoided[model][0]))

    async def _race(self, system, user, schema, max_output_tokens) -> tuple[dict, int]:
        """One answer from the configured models, inside a single deadline.

        The first call goes to the primary model unless it has just reported
        overload. A call that fails is replaced after a short growing pause,
        up to ``qa_llm_max_retries`` times. A call that has not answered after
        ``qa_llm_hedge_seconds`` gets another call racing beside it (at most
        ``_MAX_PARALLEL`` in flight). The first valid reply wins and the rest
        are cancelled, so a stalled or overloaded model cannot hold a question
        hostage. When every model is rate limited past the deadline, the
        exchange ends at once instead of spending the quota on doomed calls;
        a model whose stated rate-limit wait ends before the deadline gets one
        more call after that wait, even when the retries are used up.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.qa_gemini_timeout_seconds
        hedge = self.settings.qa_llm_hedge_seconds
        retries = self.settings.qa_llm_max_retries
        pending: dict[asyncio.Task, str] = {}
        launched = failures = replaced = hedges = 0
        replace_at = hedge_at = loop.time()
        limited: dict[str, float] = {}      # models that stated a rate-limit wait during this exchange
        failure: Exception = GenerationUnavailable("The answer model did not respond in time.")

        def launch(model: str, now: float) -> None:
            nonlocal launched, hedge_at
            limited.pop(model, None)
            pending[asyncio.create_task(self._gemini(
                system, user, schema, max_output_tokens, model, max(1.0, deadline - now)))] = model
            launched += 1
            hedge_at = now + hedge

        try:
            while True:
                now = loop.time()
                if now >= deadline:
                    raise failure
                room = len(pending) < _MAX_PARALLEL
                replacing = room and failures > replaced and replaced < retries
                hedging = room and hedge > 0 and bool(pending) and hedges < _MAX_PARALLEL - 1
                due = now if not launched else min(
                    (moment for wanted, moment in ((replacing, replace_at), (hedging, hedge_at)) if wanted),
                    default=None)
                if due is not None:
                    # A rate limit with a stated wait is obeyed, not hammered.
                    limit_ends = self._rate_limited_until(now)
                    due = max(due, limit_ends)
                    if due >= deadline and not pending:
                        if limit_ends >= deadline:
                            raise GenerationUnavailable(
                                "The answer model's rate limit was reached; it asked callers to wait "
                                f"about {math.ceil(limit_ends - now)} seconds.")
                        raise failure
                if due is not None and now >= due:
                    if launched and replacing and now >= replace_at:
                        replaced += 1
                    elif launched:
                        hedges += 1
                    launch(self._next_model(now, Counter(pending.values())), now)
                    continue
                if not pending:
                    if due is None:
                        # The retries are used up. A model that asked for a wait
                        # which ends in time is still worth one call after it.
                        waits = sorted((until, model) for model, until in limited.items()
                                       if until < deadline - 1.0)
                        if not waits:
                            raise failure
                        until, model = waits[0]
                        if until > now:
                            await self._sleep(until - now)
                        if loop.time() < until:
                            raise failure           # an injected clock did not advance
                        launch(model, loop.time())
                        continue
                    await self._sleep(min(due, deadline) - now)
                    replace_at = min(replace_at, loop.time())    # an injected clock may not advance
                    continue
                done, _ = await asyncio.wait(
                    pending, timeout=max(0.0, min(deadline, deadline if due is None else due) - now),
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    model = pending.pop(task)
                    try:
                        result = task.result()
                    except _Retryable as exc:
                        failure, failures = exc.error, failures + 1
                        if exc.missing_model:
                            self._fallback_missing = True
                        elif exc.retry_after:
                            limited[model] = loop.time() + min(_MAX_COOLDOWN_SECONDS, exc.retry_after)
                            self._avoid[model] = (limited[model], True)
                        elif exc.overloaded and len(self._gemini_models()) > 1:
                            self._avoid[model] = (loop.time() + _COOLDOWN_SECONDS, False)
                        replace_at = loop.time() + _pause(failures)
                    else:
                        self._avoid.pop(model, None)
                        return result, launched
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _openai_ready(self) -> bool:
        key = self.settings.openai_api_key
        return self._openai is not None or bool(key and key.get_secret_value())

    async def _openai_with_retries(self, system, user, schema, max_output_tokens) -> tuple[dict, int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.qa_openai_timeout_seconds
        attempts = 1 + self.settings.qa_llm_max_retries
        failure: Exception = GenerationUnavailable("The answer model did not respond in time.")
        for attempt in range(attempts):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                return await self._openai_call(system, user, schema, max_output_tokens, remaining), attempt + 1
            except _Retryable as exc:
                failure = exc.error
                if attempt + 1 < attempts:
                    await self._sleep(min(_pause(attempt + 1), max(0.0, deadline - loop.time())))
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
            wait = _retry_after(response)
            logger.info("llm_attempt_failed model=%s status=%d retry_after=%s", model, response.status_code,
                        "-" if wait is None else f"{wait:.0f}s")
            raise _Retryable(GenerationUnavailable(
                f"The answer model is temporarily unavailable (HTTP {response.status_code})."),
                overloaded=response.status_code in _OVERLOADED, retry_after=wait)
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

    async def _openai_call(self, system, user, schema, max_output_tokens, timeout: float) -> dict:
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
                timeout=max(1.0, timeout),
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
    def __init__(self, error: Exception, *, overloaded: bool = False, missing_model: bool = False,
                 retry_after: float | None = None):
        self.error = error
        self.overloaded = overloaded
        self.missing_model = missing_model
        self.retry_after = retry_after
        super().__init__(str(error))
