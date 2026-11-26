"""Structured-output calls to the answer model (Gemini or OpenAI).

One shared HTTP client is reused across requests. A model exchange is bounded
by one deadline. Inside it, a call that fails (timeout, 429/5xx, malformed
output) is replaced after a short growing pause, a call that is slow gets
another call racing beside it, a model that has just reported overload is
avoided for a while in favour of the fallback model, and a model that turns
the request down is left out of the rest of the exchange. The first valid
reply wins. Provider credentials and prompt contents are never logged or
returned.
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
# A stated wait is obeyed for at most this long, so a model whose daily quota
# is used up is still probed a few times an hour.
_MAX_COOLDOWN_SECONDS = 900.0
_MAX_PAUSE_SECONDS = 3.0     # longest wait before a failed call is replaced
_MIN_CALL_SECONDS = 1.0      # least time before the deadline that makes one more attempt worth starting


class GenerationUnavailable(RuntimeError):
    """Credentials, network or provider failure; says nothing about the law."""


class GenerationInvalid(RuntimeError):
    """The provider answered, but not with usable structured output."""


class _CredentialRejected(GenerationUnavailable):
    """Stop the exchange immediately when the provider rejects its credential."""


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


def _reading_order(call: asyncio.Task) -> int:
    """Order in which calls that finished in the same moment are read.

    Failures the exchange survives come first, so the limits they report are
    not lost when an answer arrived with them; an error that ends the exchange
    comes last, because an answer in hand is worth more.
    """
    error = call.exception()
    return 0 if isinstance(error, _Retryable) else 1 if error is None else 2


def _about(seconds: float) -> str:
    """A wait in the unit a reader expects: seconds, minutes from two minutes on, hours from two hours on."""
    for unit, size in (("hours", 3600), ("minutes", 60)):
        # A wait is quoted a moment after it was stated: 7199.9 s left of two hours is still "2 hours".
        if seconds > 2 * size - 1:
            return f"{seconds / size:.0f} {unit}"
    return f"{math.ceil(seconds)} seconds"


class AnswerGenerator:
    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None, openai_client=None,
                 sleep=asyncio.sleep):
        self.settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._openai = openai_client
        self._sleep = sleep
        self._missing_models: set[str] = set()
        # model -> (loop time until which it is avoided, whether the provider stated that wait).
        # A stated wait (rate limit) is obeyed; an assumed one (overload) only orders the models.
        self._avoid: dict[str, tuple[float, bool]] = {}
        # model -> loop time at which the wait its provider stated is over. ``_avoid`` holds the
        # capped time the model is next tried; this one is only quoted to the caller.
        self._asked: dict[str, float] = {}

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
                            max_output_tokens: int | None = None, purpose: str = "answer",
                            timeout: float | None = None) -> dict:
        """Return one JSON object that follows ``schema``.

        ``timeout`` caps the whole exchange in seconds, failover included. Each
        provider's configured time limit applies as well.
        """
        started = perf_counter()
        loop = asyncio.get_running_loop()
        ends = math.inf if timeout is None else loop.time() + timeout

        def left() -> float:
            return ends - loop.time()

        try:
            if left() <= 0:
                raise GenerationUnavailable("No time was left to ask the answer model.")
            if self.settings.use_gemini:
                try:
                    result, calls = await self._race(system, user, schema, max_output_tokens, left())
                except _CredentialRejected:
                    raise
                except (GenerationUnavailable, GenerationInvalid):
                    if not self._openai_ready() or left() < _MIN_CALL_SECONDS:
                        raise
                    # Gemini is down for this request: use the configured OpenAI model.
                    logger.warning("llm_failover purpose=%s from=gemini to=openai", purpose)
                    result, calls = await self._openai_with_retries(system, user, schema, max_output_tokens,
                                                                    left())
            else:
                result, calls = await self._openai_with_retries(system, user, schema, max_output_tokens, left())
        except (GenerationUnavailable, GenerationInvalid) as exc:
            logger.warning("llm_call_failed purpose=%s provider=%s error=%s elapsed_ms=%.0f", purpose,
                           self.provider, type(exc).__name__, (perf_counter() - started) * 1000)
            raise
        logger.info("llm_call purpose=%s provider=%s calls=%d elapsed_ms=%.0f", purpose, self.provider,
                    calls, (perf_counter() - started) * 1000)
        return result

    def _gemini_models(self) -> list[str]:
        primary, fallback = self.settings.gemini_model, self.settings.gemini_fallback_model
        if not fallback:
            return [primary]
        models = dict.fromkeys((primary, fallback, self.settings.gemini_second_fallback_model))
        return [m for m in models if m and m not in self._missing_models]

    def _stated_wait(self, model: str, now: float) -> float:
        """When the wait the provider stated for ``model`` ends, or 0 when none is running."""
        until, stated = self._avoid.get(model, (0.0, False))
        return until if stated and until > now else 0.0

    def _obey(self, model: str, now: float, wait: float) -> None:
        """Avoid ``model`` for the wait its provider stated.

        A stated wait that is still running is only ever extended. No call to the
        model starts during it, so whatever arrives meanwhile answers an older
        call and says nothing newer about the limit; only time ends the wait.
        """
        until, asked = now + min(_MAX_COOLDOWN_SECONDS, wait), now + wait
        if self._stated_wait(model, now):
            until, asked = max(until, self._avoid[model][0]), max(asked, self._asked.get(model, 0.0))
        self._avoid[model], self._asked[model] = (until, True), asked

    def _rate_limited_until(self, now: float, models: list[str]) -> float:
        """0 when one of ``models`` may be called now, else when the first rate limit ends."""
        waits = [self._stated_wait(model, now) for model in models]
        return min(waits) if waits and all(waits) else 0.0

    def _next_model(self, now: float, in_flight: Counter, models: list[str]) -> str:
        """The model for the next call: one that is not being avoided, with the fewest calls in flight."""
        avoided = {model: self._avoid.get(model, (0.0, False)) for model in models}
        healthy = [model for model in models if avoided[model][0] <= now]
        if healthy:
            return min(healthy, key=lambda model: (in_flight[model], models.index(model)))
        # Every model reported trouble: skip rate-limited ones, then take the
        # one that reported overload longest ago.
        open_models = [model for model in models if not avoided[model][1]] or models
        return min(open_models, key=lambda model: (in_flight[model], avoided[model][0]))

    async def _race(self, system, user, schema, max_output_tokens,
                    timeout: float = math.inf) -> tuple[dict, int]:
        """One answer from the configured models, inside a single deadline.

        The first call goes to the primary model unless it has just reported
        overload. A call that fails is replaced after a short growing pause,
        up to ``qa_llm_max_retries`` times. A call that has not answered after
        ``qa_llm_hedge_seconds`` gets another call racing beside it (at most
        ``_MAX_PARALLEL`` in flight). The first valid reply wins and the rest
        are cancelled, so a stalled or overloaded model cannot hold a question
        hostage. A model that turns the request down is not called again, but
        the calls in flight and the other model keep their chance to answer;
        only a rejected credential, which no model would accept, ends the
        exchange at once. When every model is rate limited past the deadline,
        the exchange ends at once instead of spending the quota on doomed
        calls; a model whose stated rate-limit wait ends before the deadline
        gets one more call after that wait, once in an exchange, even when the
        retries are used up.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.settings.qa_gemini_timeout_seconds, timeout)
        hedge = self.settings.qa_llm_hedge_seconds
        retries = self.settings.qa_llm_max_retries
        pending: dict[asyncio.Task, str] = {}
        launched = failures = replaced = hedges = 0
        attempted: set[str] = set()
        replace_at = hedge_at = loop.time()
        limited: set[str] = set()       # models that stated a rate-limit wait and were not called since
        waited: set[str] = set()        # models that already had their one call after such a wait
        refused: set[str] = set()       # models that turned this request down
        refusal: Exception | None = None
        failure: Exception = GenerationUnavailable("The answer model did not respond in time.")

        def launch(model: str, now: float) -> None:
            nonlocal launched, hedge_at
            limited.discard(model)
            attempted.add(model)
            pending[asyncio.create_task(self._gemini(
                system, user, schema, max_output_tokens, model, max(1.0, deadline - now)))] = model
            launched += 1
            hedge_at = now + hedge

        try:
            while True:
                now = loop.time()
                if now >= deadline:
                    if pending:
                        logger.warning("gemini_deadline attempts=%d pending_models=%s", launched,
                                       ",".join(sorted(set(pending.values()))))
                        raise GenerationUnavailable("Gemini did not respond in time.")
                    raise failure
                models = [model for model in self._gemini_models() if model not in refused]
                if not models and not pending:
                    raise refusal or failure
                room = bool(models) and len(pending) < _MAX_PARALLEL
                replacing = room and failures > replaced and replaced < retries
                hedging = room and hedge > 0 and bool(pending) and hedges < _MAX_PARALLEL - 1
                # An untried alternative may use an unused hedge slot after a refusal,
                # including when timed hedging and ordinary retries are disabled.
                alternate = (room and (refused or self._missing_models.intersection(attempted))
                             and not pending and not replacing and
                             hedges < _MAX_PARALLEL - 1 and any(model not in attempted for model in models))
                due = now if not launched or alternate else min(
                    (moment for wanted, moment in ((replacing, replace_at), (hedging, hedge_at)) if wanted),
                    default=None)
                # A rate limit with a stated wait is obeyed, not hammered.
                limit_ends = self._rate_limited_until(now, models)
                extra = None
                if due is not None:
                    due = max(due, limit_ends)
                elif not pending:
                    # The retries are used up. A model that asked for a wait which ends in
                    # time is still worth one call after it. Only one: a provider that answers
                    # every call with another short wait would otherwise be called until the
                    # deadline.
                    waits = sorted((max(now, self._stated_wait(model, now)), model)
                                   for model in limited - waited if model in models)
                    if waits and waits[0][0] < deadline - _MIN_CALL_SECONDS:
                        due, extra = waits[0]
                if not pending and (due is None or due >= deadline):
                    if limit_ends >= deadline:
                        asked = min(self._asked.get(model, limit_ends) for model in models)
                        raise GenerationUnavailable(
                            "The answer model's rate limit was reached; it asked callers to wait "
                            f"about {_about(asked - now)}.")
                    raise failure
                if due is not None and now >= due:
                    if extra is not None:
                        waited.add(extra)
                    elif launched and replacing and now >= replace_at:
                        replaced += 1
                    elif launched:
                        hedges += 1
                    launch(extra or self._next_model(now, Counter(pending.values()), models), now)
                    continue
                if not pending:
                    if extra is not None or due == limit_ends:
                        # A stated wait is waited out on the loop's own clock: an injected
                        # sleep that returns at once must neither skip it nor spin here.
                        await asyncio.sleep(due - now)
                    else:
                        await self._sleep(due - now)
                        replace_at = min(replace_at, loop.time())    # an injected clock may not advance
                    continue
                done, _ = await asyncio.wait(
                    pending, timeout=max(0.0, min(deadline, deadline if due is None else due) - now),
                    return_when=asyncio.FIRST_COMPLETED)
                for task in sorted(done, key=_reading_order):
                    model = pending.pop(task)
                    try:
                        result = task.result()
                    except _Retryable as exc:
                        failure, failures = exc.error, failures + 1
                        if exc.missing_model:
                            self._missing_models.add(model)
                        elif exc.refused:
                            refused.add(model)
                            refusal = exc.error
                        elif exc.retry_after:
                            self._obey(model, loop.time(), exc.retry_after)
                            limited.add(model)
                        elif (exc.overloaded and len(self._gemini_models()) > 1
                              and not self._stated_wait(model, loop.time())):
                            self._avoid[model] = (loop.time() + _COOLDOWN_SECONDS, False)
                        replace_at = loop.time() + _pause(failures)
                    else:
                        # Good news clears a cool-down that was only assumed, never a stated wait.
                        if not self._stated_wait(model, loop.time()):
                            self._avoid.pop(model, None)
                        logger.info("gemini_answer model=%s attempts=%d", model, launched)
                        return result, launched
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _openai_ready(self) -> bool:
        key = self.settings.openai_api_key
        return self._openai is not None or bool(key and key.get_secret_value())

    async def _openai_with_retries(self, system, user, schema, max_output_tokens,
                                   timeout: float = math.inf) -> tuple[dict, int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.settings.qa_openai_timeout_seconds, timeout)
        attempts = 1 + self.settings.qa_llm_max_retries
        failure: Exception = GenerationUnavailable("The answer model did not respond in time.")
        for attempt in range(attempts):
            remaining = deadline - loop.time()
            if remaining < _MIN_CALL_SECONDS:
                break               # too late for a call to answer, and none may outlive the deadline
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
            # Every model is called with the same key, so no other call would fare better.
            raise _CredentialRejected("The answer model rejected the server credential.")
        if response.status_code == 404 and model != self.settings.gemini_model:
            # The fallback model is not offered to this key: stop using it.
            raise _Retryable(GenerationUnavailable("The fallback answer model is not available."),
                             missing_model=True)
        if response.status_code != 200:
            # This model will not take the request as it is; another one may.
            logger.info("llm_attempt_failed model=%s status=%d", model, response.status_code)
            raise _Retryable(GenerationUnavailable(f"The answer model returned HTTP {response.status_code}."),
                             refused=True)
        try:
            data = response.json()
            candidate = (data.get("candidates") or [{}])[0]
            if data.get("promptFeedback", {}).get("blockReason"):
                logger.info("llm_attempt_failed model=%s error=blocked", model)
                raise _Retryable(GenerationInvalid("The answer model declined this request."), refused=True)
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
            # The SDK applies its timeout to each network phase; the call as a whole is bounded here.
            response = await asyncio.wait_for(client.responses.create(
                model=self.settings.openai_model,
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                text={"format": {"type": "json_schema", "name": "legalmind_response",
                                 "schema": schema, "strict": True}},
                max_output_tokens=max_output_tokens or self.settings.qa_openai_max_output_tokens,
                temperature=self.settings.qa_llm_temperature,
                store=False,
                timeout=timeout,
            ), timeout)
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
    """A call that failed without ending the exchange: another call may still answer.

    ``refused`` marks a model that will not take this request, ``missing_model``
    one that the key cannot use at all; neither is called again.
    """

    def __init__(self, error: Exception, *, overloaded: bool = False, missing_model: bool = False,
                 retry_after: float | None = None, refused: bool = False):
        self.error = error
        self.overloaded = overloaded
        self.missing_model = missing_model
        self.retry_after = retry_after
        self.refused = refused
        super().__init__(str(error))
