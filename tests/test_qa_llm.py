"""The answer-model client: structured output, retries, hedging and failover."""
import asyncio
import json
import selectors
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.qa.llm import AnswerGenerator, GenerationInvalid, GenerationUnavailable

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def config(**overrides):
    values = dict(
        use_gemini=True, llm_configured=True, active_model="primary-model",
        gemini_api_key=SecretStr("test-key"), gemini_model="primary-model",
        gemini_fallback_model="fallback-model", qa_gemini_timeout_seconds=5, qa_gemini_max_output_tokens=500,
        qa_gemini_thinking_level=None, qa_llm_temperature=0.2, qa_llm_max_retries=2,
        qa_llm_hedge_seconds=0.05, openai_api_key=None, openai_model="gpt-test",
        qa_openai_timeout_seconds=5, qa_openai_max_output_tokens=500)
    values.update(overrides)
    return SimpleNamespace(**values)


def reply(payload=None, *, status=200, finish="STOP", text=None):
    if status != 200:
        return httpx.Response(status, json={"error": {"message": "secret detail"}})
    body = json.dumps(payload if payload is not None else {"answer": "ok"}) if text is None else text
    return httpx.Response(200, json={"candidates": [{"finishReason": finish, "content": {"parts": [
        {"text": "thinking", "thought": True}, {"text": body}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})


async def no_sleep(_seconds):
    return None


def generator(handler, **overrides):
    return AnswerGenerator(config(**overrides), transport=httpx.MockTransport(handler), sleep=no_sleep)


def run(coroutine):
    return asyncio.run(coroutine)


class _JumpingSelector(selectors.DefaultSelector):
    """Never waits: the time the event loop meant to sleep is added to ``now`` instead."""

    now = 1000.0            # far from zero, so that sleeping until a moment lands on it exactly

    def select(self, timeout=None):
        self.now += timeout or 0.0
        return super().select(0)


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps over every wait: a 25-second exchange is over at once."""

    def __init__(self):
        self._clock = _JumpingSelector()
        super().__init__(self._clock)

    def time(self):
        return self._clock.now


def run_virtual(coroutine):
    return asyncio.run(coroutine, loop_factory=VirtualClockLoop)


async def timed(awaitable):
    """What ``awaitable`` returned, or the generation error it raised, and the seconds it took."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        outcome = await awaitable
    except (GenerationUnavailable, GenerationInvalid) as exc:
        outcome = exc
    return outcome, loop.time() - started


def test_request_shape_and_structured_reply():
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return reply({"answer": "grounded"})
    client = generator(handler, qa_gemini_thinking_level="minimal")
    assert run(client.generate_json("system rules", "user data", SCHEMA)) == {"answer": "grounded"}
    assert seen["url"].endswith("/models/primary-model:generateContent") and seen["key"] == "test-key"
    body = seen["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "system rules"
    assert body["contents"][0]["parts"][0]["text"] == "user data"
    assert body["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert body["generationConfig"]["temperature"] == 0.2
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    assert "test-key" not in seen["url"]
    run(client.aclose())


def test_an_overloaded_primary_is_replaced_by_the_fallback_model():
    calls = []

    def handler(request):
        model = str(request.url).split("/models/")[1].split(":")[0]
        calls.append(model)
        return reply(status=503) if model == "primary-model" else reply({"answer": "from fallback"})
    assert run(generator(handler).generate_json("s", "u", SCHEMA)) == {"answer": "from fallback"}
    assert calls == ["primary-model", "fallback-model"]


def test_a_stalled_primary_is_raced_by_the_fallback_model():
    calls = []

    async def handler(request):
        model = str(request.url).split("/models/")[1].split(":")[0]
        calls.append(model)
        if model == "primary-model":
            await asyncio.sleep(30)                 # never answers in time
        return reply({"answer": model})
    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        result = started.run_until_complete(generator(handler).generate_json("s", "u", SCHEMA))
    finally:
        started.close()
    assert result == {"answer": "fallback-model"} and calls == ["primary-model", "fallback-model"]


def model_of(request):
    return str(request.url).split("/models/")[1].split(":")[0]


def test_every_stalled_call_gets_company_until_one_answers():
    calls = []

    async def handler(request):
        calls.append(model_of(request))
        if len(calls) < 3:
            await asyncio.sleep(30)                 # the first two calls hang
        return reply({"answer": f"call {len(calls)}"})
    assert run(generator(handler).generate_json("s", "u", SCHEMA)) == {"answer": "call 3"}
    # Calls alternate between the models so a hang on one does not block the other.
    assert calls == ["primary-model", "fallback-model", "primary-model"]


def test_a_model_that_reported_overload_is_avoided_for_the_next_questions():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return reply(status=503) if calls[-1] == "primary-model" else reply({"answer": "ok"})
    client = generator(handler)
    for _ in range(3):
        assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "ok"}
    # Only the first question paid for a failed call to the overloaded model.
    assert calls == ["primary-model", "fallback-model", "fallback-model", "fallback-model"]
    # Once the cool-down passes, the primary model is tried first again.
    client._avoid["primary-model"] = (0.0, False)
    run(client.generate_json("s", "u", SCHEMA))
    assert calls[-2:] == ["primary-model", "fallback-model"]


def test_the_wait_a_rate_limited_model_asks_for_is_respected():
    def handler(request):
        if model_of(request) == "primary-model":
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "47s"}]}})
        return reply({"answer": "ok"})
    client = generator(handler)

    async def ask():
        result = await client.generate_json("s", "u", SCHEMA)
        until, stated = client._avoid["primary-model"]
        return result, until - asyncio.get_running_loop().time(), stated
    result, wait, stated = run(ask())
    assert result == {"answer": "ok"} and 40 < wait <= 47 and stated


def rate_limited(seconds):
    return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{seconds}s"}]}})


def test_a_rate_limit_longer_than_the_deadline_ends_the_exchange_at_once():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return rate_limited(60)
    client = generator(handler)
    with pytest.raises(GenerationUnavailable, match="rate limit was reached"):
        run(client.generate_json("s", "u", SCHEMA))
    assert calls == ["primary-model", "fallback-model"]     # one call each, then no hammering
    # The next question does not call a model that is still rate limited, and says why.
    with pytest.raises(GenerationUnavailable, match=r"wait about (59|60) seconds"):
        run(client.generate_json("s", "u", SCHEMA))
    assert len(calls) == 2


def test_a_rate_limited_model_gets_one_more_call_after_its_wait_even_without_retries_left():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        if calls[-1] == "primary-model":
            return rate_limited(0.6) if calls.count("primary-model") == 1 else reply({"answer": "second chance"})
        return reply(status=503)
    # One retry only: it is spent on the overloaded fallback long before the wait is over.
    client = AnswerGenerator(config(qa_llm_hedge_seconds=0, qa_llm_max_retries=1),
                             transport=httpx.MockTransport(handler))
    assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "second chance"}
    assert calls == ["primary-model", "fallback-model", "primary-model"]


def test_a_short_rate_limit_is_waited_out_and_overloaded_models_are_preferred_meanwhile():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        if calls[-1] == "primary-model":
            return rate_limited(0.5) if calls.count("primary-model") == 1 else reply({"answer": "after the wait"})
        return reply(status=503)
    client = AnswerGenerator(config(qa_llm_hedge_seconds=0), transport=httpx.MockTransport(handler))
    assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "after the wait"}
    # While the primary is rate limited only the overloaded fallback is retried.
    assert calls[0] == "primary-model" and calls[-1] == "primary-model"
    assert set(calls[1:-1]) == {"fallback-model"}


@pytest.mark.parametrize("overrides, expected", [
    (dict(gemini_fallback_model=None, qa_llm_max_retries=0), 2),
    (dict(qa_llm_max_retries=2), 5),
])
def test_a_provider_that_always_asks_for_a_short_wait_is_not_called_until_the_deadline(overrides, expected):
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return rate_limited(1)
    client = AnswerGenerator(config(qa_gemini_timeout_seconds=25, **overrides), transport=httpx.MockTransport(handler))
    outcome, seconds = run_virtual(timed(client.generate_json("s", "u", SCHEMA)))
    assert isinstance(outcome, GenerationUnavailable) and seconds < 25
    # The first call, the retries, and one more call per model after the wait it asked for.
    assert len(calls) == expected


def test_a_stated_wait_is_waited_out_even_when_pauses_are_skipped():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return rate_limited(2) if len(calls) == 1 else reply({"answer": "after the wait"})
    client = generator(handler, gemini_fallback_model=None, qa_llm_max_retries=0)
    outcome, seconds = run_virtual(timed(client.generate_json("s", "u", SCHEMA)))
    assert outcome == {"answer": "after the wait"} and seconds == pytest.approx(2)


@pytest.mark.parametrize("older_call", ["answers", "is overloaded", "asks for a shorter wait"])
def test_only_time_ends_a_stated_wait(older_call):
    calls = []

    async def handler(request):
        calls.append(model_of(request))
        if calls[-1] == "fallback-model":
            return reply({"answer": "fallback"})
        if calls.count("primary-model") == 1:
            await asyncio.sleep(3)                  # started before the limit, finished after it was stated
            return {"answers": reply({"answer": "primary"}), "is overloaded": reply(status=503),
                    "asks for a shorter wait": rate_limited(2)}[older_call]
        return rate_limited(60) if calls.count("primary-model") == 2 else reply({"answer": "primary again"})
    client = AnswerGenerator(config(qa_llm_hedge_seconds=0), transport=httpx.MockTransport(handler))

    async def questions():
        loop = asyncio.get_running_loop()
        started = loop.time()
        older = asyncio.ensure_future(client.generate_json("s", "u", SCHEMA))
        await asyncio.sleep(1)
        await client.generate_json("s", "u", SCHEMA)           # the primary says: wait 60 seconds
        await older
        answers = []
        for moment in (30, 70):
            await asyncio.sleep(started + moment - loop.time())
            answers.append((await client.generate_json("s", "u", SCHEMA), calls.count("primary-model")))
        return answers
    during, after = run_virtual(questions())
    # Half a minute later the primary is still left alone, whatever its older call came back with.
    assert during == ({"answer": "fallback"}, 2)
    assert after == ({"answer": "primary again"}, 3)


@pytest.mark.parametrize("shift", [0, 1, 2])
def test_calls_that_finish_in_the_same_moment_keep_both_the_answer_and_the_stated_wait(shift):
    calls = []

    async def handler(request):
        calls.append(model_of(request))
        call = len(calls)
        await asyncio.sleep(3.5 - call)             # started a second apart, so all three end together
        outcome = ("answer", "wait", "credential")[(call + shift) % 3]
        return {"answer": reply(), "wait": rate_limited(60), "credential": reply(status=403)}[outcome]
    client = AnswerGenerator(config(gemini_fallback_model=None, qa_llm_hedge_seconds=1),
                             transport=httpx.MockTransport(handler))

    async def questions():
        return await client.generate_json("s", "u", SCHEMA), (await timed(client.generate_json("s", "u", SCHEMA)))[0]
    first, second = run_virtual(questions())
    assert first == {"answer": "ok"}                # the rejected credential did not cost the answer
    assert isinstance(second, GenerationUnavailable) and "rate limit was reached" in str(second) and len(calls) == 3


@pytest.mark.parametrize("primary, fallback, wording", [
    (600, 600, "10 minutes"), (86400, 86400, "24 hours"), (7200, 7200, "2 hours"), (3600, 180, "3 minutes"),
])
def test_the_rate_limit_error_quotes_the_wait_the_provider_asked_for(primary, fallback, wording):
    def handler(request):
        return rate_limited(primary if model_of(request) == "primary-model" else fallback)
    with pytest.raises(GenerationUnavailable, match=f"rate limit was reached.* wait about {wording}"):
        run(generator(handler).generate_json("s", "u", SCHEMA))


def test_a_lone_rate_limited_model_reports_its_wait_even_without_retries():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return rate_limited(60)
    client = generator(handler, gemini_fallback_model=None, qa_llm_max_retries=0)
    with pytest.raises(GenerationUnavailable, match="rate limit was reached.* wait about 60 seconds"):
        run(client.generate_json("s", "u", SCHEMA))
    assert len(calls) == 1


def test_a_model_out_of_quota_is_probed_again_after_a_quarter_of_an_hour():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        if calls == ["primary-model"]:
            return rate_limited(86400)              # the daily quota is used up
        return reply({"answer": calls[-1]})
    client = AnswerGenerator(config(), transport=httpx.MockTransport(handler))

    async def questions():
        answers = []
        for pause in (0, 600, 400):
            await asyncio.sleep(pause)
            answers.append(await client.generate_json("s", "u", SCHEMA))
        return answers
    assert [answer["answer"] for answer in run_virtual(questions())] == [
        "fallback-model", "fallback-model", "primary-model"]


def test_when_every_model_is_overloaded_they_take_turns_until_the_retries_run_out():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return reply(status=503)
    with pytest.raises(GenerationUnavailable):
        run(generator(handler, qa_llm_max_retries=3).generate_json("s", "u", SCHEMA))
    assert calls == ["primary-model", "fallback-model", "primary-model", "fallback-model"]


def test_a_success_clears_the_overload_mark():
    outcomes = iter([503, 200, 200])

    def handler(request):
        return reply(status=503) if next(outcomes) == 503 else reply({"answer": model_of(request)})
    client = generator(handler, gemini_fallback_model=None)
    assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "primary-model"}
    assert not client._avoid                     # a lone model is not avoided for overload


def test_transient_errors_are_retried_then_reported_as_unavailable():
    attempts = []

    def flaky(request):
        attempts.append(1)
        return reply(status=500) if len(attempts) < 3 else reply({"answer": "third time"})
    assert run(generator(flaky, gemini_fallback_model=None).generate_json("s", "u", SCHEMA)) == {"answer": "third time"}

    def down(request):
        return reply(status=503)
    with pytest.raises(GenerationUnavailable) as failure:
        run(generator(down).generate_json("s", "u", SCHEMA))
    assert "secret detail" not in str(failure.value) and "test-key" not in str(failure.value)


def test_network_errors_and_deadline_are_unavailable_not_evidence_gaps():
    def unreachable(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(GenerationUnavailable):
        run(generator(unreachable).generate_json("s", "u", SCHEMA))

    async def hangs(request):
        await asyncio.sleep(30)
    with pytest.raises(GenerationUnavailable):
        run(generator(hangs, qa_gemini_timeout_seconds=0.3, gemini_fallback_model=None).generate_json("s", "u", SCHEMA))


@pytest.mark.parametrize("status", [401, 403])
def test_credential_errors_fail_immediately(status):
    calls = []

    def handler(request):
        calls.append(1)
        return reply(status=status)
    with pytest.raises(GenerationUnavailable):
        run(generator(handler).generate_json("s", "u", SCHEMA))
    assert len(calls) == 1


def refuse(kind):
    """A reply with which a model turns the request itself down, so asking that model again is pointless."""
    if kind == "blocked":
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
    return reply(status=int(kind))


@pytest.mark.parametrize("kind", ["400", "404", "blocked"])
def test_a_request_the_primary_refuses_is_answered_by_the_fallback(kind):
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return refuse(kind) if calls[-1] == "primary-model" else reply({"answer": "from fallback"})
    client = generator(handler)
    assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "from fallback"}
    assert calls == ["primary-model", "fallback-model"]
    # The refusal concerned that request only: the next one starts with the primary again.
    run(client.generate_json("s", "u", SCHEMA))
    assert calls[2:] == ["primary-model", "fallback-model"]


@pytest.mark.parametrize("kind", ["400", "blocked"])
def test_a_refused_hedge_does_not_discard_the_answer_that_is_on_its_way(kind):
    calls = []

    async def handler(request):
        calls.append(model_of(request))
        if calls[-1] == "fallback-model":
            return refuse(kind)
        await asyncio.sleep(5)                      # slower than the hedge, well inside the deadline
        return reply({"answer": "from primary"})
    client = AnswerGenerator(config(qa_gemini_timeout_seconds=25, qa_llm_hedge_seconds=3),
                             transport=httpx.MockTransport(handler))
    outcome, seconds = run_virtual(timed(client.generate_json("s", "u", SCHEMA)))
    assert outcome == {"answer": "from primary"} and seconds == pytest.approx(5)
    assert calls.count("fallback-model") == 1       # the model that refused is left alone


def test_a_model_that_refused_is_left_out_while_the_other_is_retried_as_usual():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return reply(status=400) if calls[-1] == "primary-model" else reply(status=503)
    # The fallback could still have answered, so its trouble is what the caller is told.
    with pytest.raises(GenerationUnavailable, match="HTTP 503"):
        run(generator(handler).generate_json("s", "u", SCHEMA))
    assert calls == ["primary-model", "fallback-model", "fallback-model"]


@pytest.mark.parametrize("kind, error", [("400", GenerationUnavailable), ("blocked", GenerationInvalid)])
def test_a_request_every_model_refuses_fails_with_the_refusal(kind, error):
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return refuse(kind)
    with pytest.raises(error):
        run(generator(handler).generate_json("s", "u", SCHEMA))
    assert calls == ["primary-model", "fallback-model"]     # each model was asked once
    # A lone model has no other model to hand the request to.
    with pytest.raises(error):
        run(generator(handler, gemini_fallback_model=None).generate_json("s", "u", SCHEMA))
    assert calls[2:] == ["primary-model"]


def test_a_fallback_model_the_key_cannot_use_is_dropped():
    calls = []

    def handler(request):
        model = str(request.url).split("/models/")[1].split(":")[0]
        calls.append(model)
        if model == "fallback-model":
            return reply(status=404)
        return reply(status=503) if calls.count("primary-model") == 1 else reply({"answer": "recovered"})
    client = generator(handler)
    assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "recovered"}
    assert client._fallback_missing and calls.count("fallback-model") == 1


@pytest.mark.parametrize("bad", [
    dict(text="not json at all"), dict(finish="MAX_TOKENS"), dict(text=""), dict(text="[1, 2]"),
])
def test_malformed_output_is_retried_and_finally_invalid(bad):
    calls = []

    def handler(request):
        calls.append(1)
        return reply(**bad)
    with pytest.raises(GenerationInvalid):
        run(generator(handler, gemini_fallback_model=None).generate_json("s", "u", SCHEMA))
    assert len(calls) == 3                       # the first call plus qa_llm_max_retries replacements


def test_json_wrapped_in_a_code_fence_is_accepted():
    def handler(request):
        return reply(text='```json\n{"answer": "fenced"}\n```')
    assert run(generator(handler).generate_json("s", "u", SCHEMA)) == {"answer": "fenced"}


def test_blocked_prompt_and_missing_key():
    def blocked(request):
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
    with pytest.raises(GenerationInvalid):
        run(generator(blocked, gemini_fallback_model=None, qa_llm_max_retries=0).generate_json("s", "u", SCHEMA))
    missing = AnswerGenerator(config(gemini_api_key=SecretStr(""), llm_configured=False), sleep=no_sleep)
    assert not missing.configured
    with pytest.raises(GenerationUnavailable, match="GEMINI_API_KEY"):
        run(missing.generate_json("s", "u", SCHEMA))


class FakeOpenAI:
    def __init__(self, outcomes, *, seconds=0):
        self.outcomes, self.requests, self.closed = list(outcomes), [], False
        self.seconds = seconds                      # how long every call takes, whatever its timeout
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        assert kwargs["timeout"] > 0                # each call is bounded by the exchange deadline
        self.requests.append(kwargs)
        if self.seconds:
            await asyncio.sleep(self.seconds)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self):
        self.closed = True


def completed(text):
    return SimpleNamespace(status="completed", output_text=text)


def test_openai_structured_output_and_retry():
    class RateLimitError(Exception):
        pass
    client = FakeOpenAI([RateLimitError("slow down"), completed('{"answer": "from openai"}')])
    api = AnswerGenerator(config(use_gemini=False, active_model="gpt-test"), openai_client=client, sleep=no_sleep)
    assert api.provider == "openai" and api.configured
    assert run(api.generate_json("s", "u", SCHEMA)) == {"answer": "from openai"}
    request = client.requests[-1]
    assert request["model"] == "gpt-test" and request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema" and request["text"]["format"]["schema"] == SCHEMA
    run(api.aclose())
    assert client.closed


def test_openai_credential_errors_and_refusals():
    class AuthenticationError(Exception):
        pass
    rejected = AnswerGenerator(config(use_gemini=False), openai_client=FakeOpenAI([AuthenticationError("sk-secret")]),
                               sleep=no_sleep)
    with pytest.raises(GenerationUnavailable) as failure:
        run(rejected.generate_json("s", "u", SCHEMA))
    assert "sk-secret" not in str(failure.value)
    refused = AnswerGenerator(config(use_gemini=False, qa_llm_max_retries=0),
                              openai_client=FakeOpenAI([completed("")]), sleep=no_sleep)
    with pytest.raises(GenerationInvalid):
        run(refused.generate_json("s", "u", SCHEMA))


def test_gemini_outage_fails_over_to_a_configured_openai_model():
    def down(request):
        return reply(status=503)
    client = FakeOpenAI([completed('{"answer": "failover"}')])
    api = AnswerGenerator(config(), transport=httpx.MockTransport(down), openai_client=client, sleep=no_sleep)
    assert run(api.generate_json("s", "u", SCHEMA)) == {"answer": "failover"}


def test_no_openai_attempt_starts_too_late_to_answer_before_the_deadline():
    class RateLimitError(Exception):
        pass
    client = FakeOpenAI([RateLimitError("slow down"), completed('{"answer": "too late"}')], seconds=4.5)
    api = AnswerGenerator(config(use_gemini=False), openai_client=client, sleep=no_sleep)
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA)))
    assert isinstance(outcome, GenerationUnavailable) and seconds <= 5
    assert len(client.requests) == 1                # half a second was left: too little for another call


def test_an_openai_call_that_outlasts_its_timeout_is_cut_off_at_the_deadline():
    client = FakeOpenAI([completed('{"answer": "too late"}')], seconds=30)
    api = AnswerGenerator(config(use_gemini=False), openai_client=client, sleep=no_sleep)
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA)))
    assert isinstance(outcome, GenerationUnavailable) and seconds == pytest.approx(5)
    assert len(client.requests) == 1


async def hangs(request):
    await asyncio.sleep(60)


def test_a_caller_timeout_caps_the_exchange_and_never_lifts_the_configured_limit():
    api = AnswerGenerator(config(), transport=httpx.MockTransport(hangs))
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA, timeout=2)))
    assert isinstance(outcome, GenerationUnavailable) and seconds == pytest.approx(2)
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA, timeout=60)))
    assert isinstance(outcome, GenerationUnavailable) and seconds == pytest.approx(5)
    slow = FakeOpenAI([completed('{"answer": "too late"}')], seconds=30)
    api = AnswerGenerator(config(use_gemini=False), openai_client=slow)
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA, timeout=2)))
    assert isinstance(outcome, GenerationUnavailable) and seconds == pytest.approx(2)
    # A timeout that is long enough changes nothing.
    assert run(generator(lambda request: reply()).generate_json("s", "u", SCHEMA, timeout=30)) == {"answer": "ok"}


def test_failover_gets_only_what_is_left_of_the_caller_timeout():
    client = FakeOpenAI([completed('{"answer": "too late"}')], seconds=30)
    api = AnswerGenerator(config(), transport=httpx.MockTransport(hangs), openai_client=client)
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA, timeout=8)))
    # Gemini used up its 5 seconds, so OpenAI was given the remaining 3 instead of its own 5.
    assert isinstance(outcome, GenerationUnavailable) and seconds == pytest.approx(8)
    assert [request["timeout"] for request in client.requests] == [pytest.approx(3)]
    # With too little left for a call there is no failover, and Gemini's own error is reported.
    outcome, seconds = run_virtual(timed(api.generate_json("s", "u", SCHEMA, timeout=5.5)))
    assert "did not respond in time" in str(outcome) and seconds == pytest.approx(5)
    assert len(client.requests) == 1


@pytest.mark.parametrize("timeout", [0, -1])
def test_an_exchange_without_time_calls_no_model(timeout):
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return reply()
    client = FakeOpenAI([completed('{"answer": "unused"}')])
    for api in (AnswerGenerator(config(), transport=httpx.MockTransport(handler), openai_client=client),
                AnswerGenerator(config(use_gemini=False), openai_client=client)):
        with pytest.raises(GenerationUnavailable):
            run(api.generate_json("s", "u", SCHEMA, timeout=timeout))
    assert not calls and not client.requests


def test_refusal_still_tries_an_untried_model_with_no_retries_or_hedges():
    calls = []

    def handler(request):
        calls.append(model_of(request))
        return reply(status=400) if len(calls) == 1 else reply()

    client = generator(handler, qa_llm_max_retries=0, qa_llm_hedge_seconds=0)
    assert run(client.generate_json("s", "u", SCHEMA)) == {"answer": "ok"}
    assert calls == ["primary-model", "fallback-model"]


def test_credential_rejection_ends_the_exchange_even_with_failover_configured():
    client = generator(lambda request: reply(status=403))
    client._openai = SimpleNamespace(responses=None)
    with pytest.raises(GenerationUnavailable, match="credential"):
        run(client.generate_json("s", "u", SCHEMA))
