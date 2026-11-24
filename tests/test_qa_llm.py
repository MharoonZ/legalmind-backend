"""The answer-model client: structured output, retries, hedging and failover."""
import asyncio
import json
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


@pytest.mark.parametrize("status", [401, 403, 400])
def test_credential_and_request_errors_fail_immediately(status):
    calls = []

    def handler(request):
        calls.append(1)
        return reply(status=status)
    with pytest.raises(GenerationUnavailable):
        run(generator(handler).generate_json("s", "u", SCHEMA))
    assert len(calls) == 1


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
    def __init__(self, outcomes):
        self.outcomes, self.requests, self.closed = list(outcomes), [], False
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        assert kwargs["timeout"] > 0                # each call is bounded by the exchange deadline
        self.requests.append(kwargs)
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
