"""Gemini HTTP contract checks using an in-memory provider."""
import asyncio
import json

import httpx
import pytest

from app.core.qa_generation_config import AnswerSettings
from app.services.qa.generation import AnswerGenerator, GenerationInvalid, GenerationUnavailable
from tests.test_qa_generation import PACKET


def generator(handler, **settings):
    return AnswerGenerator(AnswerSettings(_env_file=None, llm_provider="gemini",
        gemini_api_key="private-test-key", gemini_model="gemini-3.1-flash-lite", **settings), gemini_transport=httpx.MockTransport(handler))


def draft_response(**candidate):
    draft = json.dumps({"outcome": "insufficient_evidence", "answer": "", "claims": [],
                        "missing_support": "A verified source is unavailable.", "clarification_question": None})
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"thought": True, "text": "provider reasoning"}, {"text": draft[:31]}, {"text": draft[31:]}
    ]}, **candidate}], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 24}}


def test_gemini_uses_server_header_structured_schema_and_configured_budget():
    def handler(request):
        assert request.url.path == "/v1beta/models/gemini-3.1-flash-lite:generateContent"
        assert not request.url.query
        assert request.headers["x-goog-api-key"] == "private-test-key"
        body = json.loads(request.content)
        assert body["systemInstruction"]["parts"] == [{"text": PACKET.system}]
        assert body["contents"] == [{"role": "user", "parts": [{"text": PACKET.user}]}]
        config = body["generationConfig"]
        assert config["maxOutputTokens"] == 1777 and config["responseMimeType"] == "application/json"
        assert config["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
        assert config["responseJsonSchema"]["additionalProperties"] is False
        assert "clarification_question" in config["responseJsonSchema"]["required"]
        assert "private-test-key" not in request.content.decode()
        return httpx.Response(200, json=draft_response())

    adapter = generator(handler, qa_gemini_max_output_tokens=1777, qa_gemini_thinking_level="minimal")
    assert adapter.settings.active_model == "gemini-3.1-flash-lite"
    draft = asyncio.run(adapter.generate(PACKET))
    assert draft.outcome == "insufficient_evidence" and not draft.claims


@pytest.mark.parametrize("candidate", [
    {"finishReason": "MAX_TOKENS"}, {"finishReason": "SAFETY"},
    {"content": {"parts": [{"text": '{"outcome":"answered"}'}]}},
    {"content": {"parts": [{"thought": True, "text": "{}"}]}},
])
def test_gemini_incomplete_blocked_or_invalid_output_is_rejected(candidate):
    adapter = generator(lambda request: httpx.Response(200, json=draft_response(**candidate)))
    with pytest.raises(GenerationInvalid):
        asyncio.run(adapter.generate(PACKET))


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_gemini_errors_do_not_echo_provider_details_or_retry_generation(status, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private provider diagnostic")

    with pytest.raises(GenerationUnavailable) as failure:
        asyncio.run(generator(handler).generate(PACKET))
    assert "private" not in str(failure.value)
    assert "private provider diagnostic" not in caplog.text
    assert len(calls) == 1


def test_gemini_request_deadline_is_absolute_and_redacts_transport_details():
    async def handler(request):
        await asyncio.sleep(1)
        pytest.fail("The provider deadline was not applied.")

    with pytest.raises(GenerationUnavailable) as failure:
        asyncio.run(generator(handler, qa_gemini_timeout_seconds=.01).generate(PACKET))
    assert failure.value.__cause__ is None


def test_explicit_gemini_without_a_key_does_not_fall_back_to_openai():
    adapter = AnswerGenerator(AnswerSettings(_env_file=None, llm_provider="gemini",
        gemini_api_key=None, openai_api_key="unused"))
    with pytest.raises(GenerationUnavailable, match="GEMINI_API_KEY"):
        asyncio.run(adapter.generate(PACKET))
