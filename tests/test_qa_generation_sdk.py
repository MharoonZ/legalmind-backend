"""Exercise the installed OpenAI SDK through in-memory HTTP, never the network."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
import pytest

from app.api.routes.qa import get_question_answer_service
from app.core.qa_generation_config import AnswerSettings
from app.main import create_app
from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa.answer import QuestionAnswerService
from app.services.qa.citations import CitationVerifier
from app.services.qa.generation import AnswerGenerator, GenerationInvalid, GenerationUnavailable
from app.services.qa.prompt import PromptPacket
from tests.test_qa_answer import FakePrep, ready
from tests.test_qa_citations import FakeSource
from tests.test_qa_generation import FakeResponses


PACKET = PromptPacket(system="Use only supplied evidence.", user="Synthetic question and evidence.",
                      evidence_ids=("chunk:synthetic",))
PRIVATE = "PRIVATE-PROVIDER-DETAIL-DO-NOT-RETURN"


def draft_data(outcome="answered"):
    return dict(outcome=outcome, answer="Unverified model prose.", analysis=[],
        claims=[] if outcome == "insufficient_evidence" else [dict(evidence_id="chunk:synthetic",
            quote="Provided that a signed permit is required.",
            claim_text="Provided that a signed permit is required.")],
        missing_support="" if outcome == "answered" else "Whether the user has a signed permit.",
        clarification_question="Do you have a signed permit?" if outcome == "clarification_needed" else None)


def response_body(data=None, *, text=None, status="completed", content=None):
    if content is None:
        content = [dict(type="output_text", text=text if text is not None else json.dumps(
            data if data is not None else draft_data()), annotations=[])]
    return dict(id="resp_synthetic", object="response", created_at=1, status=status,
        model="gpt-6.1-sol", error=None, incomplete_details=None,
        output=[dict(type="message", id="msg_synthetic", role="assistant", status="completed", content=content)],
        usage=dict(input_tokens=100, output_tokens=40, total_tokens=140,
                   input_tokens_details=dict(cached_tokens=0), output_tokens_details=dict(reasoning_tokens=0)))


@asynccontextmanager
async def sdk_adapter(bodies, *, http_status=200, error_factory=None):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if error_factory:
            raise error_factory(request)
        body = bodies[min(len(calls) - 1, len(bodies) - 1)]
        return httpx.Response(http_status, json=body)
    async with AsyncOpenAI(api_key="sk-synthetic-test-only", base_url="https://provider.invalid/v1",
            max_retries=0, timeout=4,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        settings = AnswerSettings(_env_file=None, openai_api_key=None, openai_model="gpt-6.1-sol",
                                  qa_openai_timeout_seconds=4, qa_openai_max_output_tokens=1357)
        yield AnswerGenerator(settings, client=client), calls


@pytest.mark.parametrize("outcome", ["answered", "partial", "clarification_needed", "insufficient_evidence"])
def test_real_sdk_parses_each_supported_outcome_and_sends_configured_contract(outcome):
    async def scenario():
        data = draft_data(outcome)
        async with sdk_adapter([response_body(data)]) as (generator, calls):
            parsed = await generator.generate(PACKET)
        assert parsed.model_dump() == data and len(calls) == 1
        sent = calls[0]
        assert sent["model"] == "gpt-6.1-sol" and sent["max_output_tokens"] == 1357
        assert sent["store"] is False
        assert sent["input"] == [{"role": "system", "content": PACKET.system},
                                 {"role": "user", "content": PACKET.user}]
        assert not sent.get("tools") and not sent.get("previous_response_id")
        schema = sent["text"]["format"]
        assert schema["type"] == "json_schema" and schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert set(schema["schema"]["required"]) == set(AnswerDraft.model_fields)
        assert "sk-synthetic-test-only" not in json.dumps(sent)
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["{", "not JSON " + PRIVATE, '{"outcome":"answered"}',
    json.dumps(dict(draft_data(), claims=None)),
    json.dumps(dict(draft_data(), outcome="made_up")),
    json.dumps(dict(draft_data(), model_supplied_url="https://example.invalid/invented"))])
def test_real_sdk_parse_failures_are_invalid_drafts_not_provider_outages(text):
    async def scenario():
        async with sdk_adapter([response_body(text=text)]) as (generator, calls):
            with pytest.raises(GenerationInvalid) as failure:
                await generator.generate(PACKET)
            assert PRIVATE not in str(failure.value) and len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled", "queued", "in_progress"])
def test_noncompleted_real_sdk_response_never_returns_even_parseable_draft(status):
    async def scenario():
        async with sdk_adapter([response_body(status=status)]) as (generator, calls):
            with pytest.raises(GenerationInvalid):
                await generator.generate(PACKET)
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("with_answer", [False, True])
def test_explicit_refusal_is_rejected_even_if_other_content_has_valid_json(with_answer):
    content = [dict(type="refusal", refusal=PRIVATE)]
    if with_answer:
        content.extend(response_body()["output"][0]["content"])
    async def scenario():
        async with sdk_adapter([response_body(content=content)]) as (generator, calls):
            with pytest.raises(GenerationInvalid) as failure:
                await generator.generate(PACKET)
            assert PRIVATE not in str(failure.value) and len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["outcome", "nested_claim"])
def test_preconstructed_model_instances_are_revalidated(fault):
    draft = AnswerDraft.model_validate(draft_data())
    if fault == "outcome":
        draft = draft.model_copy(update={"outcome": "made_up"})
    else:
        draft = draft.model_copy(update={"claims": [DraftClaim.model_construct(
            evidence_id="chunk:synthetic", claim_text="A quote field is missing.")]})
    generator = AnswerGenerator(AnswerSettings(_env_file=None), client=SimpleNamespace(
        responses=FakeResponses(SimpleNamespace(status="completed", output_parsed=draft))))
    with pytest.raises(GenerationInvalid):
        asyncio.run(generator.generate(PACKET))


@pytest.mark.parametrize("status", [400, 401, 403, 408, 409, 422, 429, 500, 503])
def test_real_sdk_http_errors_are_private_bounded_service_failures(status):
    async def scenario():
        async with sdk_adapter([dict(error=dict(message=PRIVATE, type="synthetic_error", code=None))],
                               http_status=status) as (generator, calls):
            with pytest.raises(GenerationUnavailable) as failure:
                await generator.generate(PACKET)
            assert PRIVATE not in str(failure.value) and len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_real_sdk_transport_failures_do_not_retry_or_expose_details(error_type):
    async def scenario():
        async with sdk_adapter([], error_factory=lambda request: error_type(PRIVATE, request=request)) as (generator, calls):
            with pytest.raises(GenerationUnavailable) as failure:
                await generator.generate(PACKET)
            assert PRIVATE not in str(failure.value) and len(calls) == 1
    asyncio.run(scenario())


def test_default_client_uses_configured_timeout_and_zero_sdk_retries_and_is_reused(monkeypatch):
    captured = []
    transport = httpx.MockTransport(lambda request: pytest.fail("No provider request was expected."))
    def factory(**kwargs):
        captured.append(kwargs)
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport))
    monkeypatch.setattr("openai.AsyncOpenAI", factory)
    settings = AnswerSettings(_env_file=None, openai_api_key="sk-synthetic-test-only",
                              qa_openai_timeout_seconds=7.5)
    generator = AnswerGenerator(settings)
    first = generator._get_client()
    assert generator._get_client() is first
    assert len(captured) == 1 and captured[0]["max_retries"] == 0
    assert captured[0]["timeout"] == 7.5
    asyncio.run(first.close())


async def ask_api(generator, decision, source):
    app = create_app()
    service = QuestionAnswerService(FakePrep(decision), generator, CitationVerifier(source), repair_attempts=1)
    app.dependency_overrides[get_question_answer_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/ask", json={"question": "What does the synthetic section say?"})


@pytest.mark.parametrize("case", ["valid", "repair", "reject"])
def test_prompt_sdk_and_api_return_only_verified_quotes_with_bounded_citation_repair(case):
    decision, draft = ready()
    valid = draft.model_dump()
    invalid = dict(valid, claims=[dict(valid["claims"][0], evidence_id="chunk:invented")])
    bodies = [response_body(valid)] if case == "valid" else [response_body(invalid),
        response_body(valid if case == "repair" else invalid)]
    async def scenario():
        source = FakeSource()
        async with sdk_adapter(bodies) as (generator, calls):
            result = await ask_api(generator, decision, source)
        assert result.status_code == 200
        public = result.json()
        assert draft.answer not in public["answer"]
        assert len(calls) == (1 if case == "valid" else 2)
        sent = json.loads(calls[0]["input"][1]["content"].split("\n", 1)[1])
        assert [u["text"] for u in sent["evidence"]] == [u.text for u in decision.context.units]
        if case == "reject":
            assert public["status"] == "insufficient_evidence" and not public["citations"]
            assert source.calls == []
        else:
            assert public["status"] == "answered" and public["citations"]
            assert draft.claims[0].quote in public["answer"] and len(source.calls) == 1
        if case != "valid":
            assert "invalid_evidence_id" in calls[1]["input"][1]["content"]
            assert calls[0]["input"][0] == calls[1]["input"][0]
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["invalid_json", "invalid_schema", "refusal", "incomplete", "provider_error"])
def test_real_sdk_generation_failure_is_public_503_without_claims_or_secret_details(case):
    decision, _ = ready()
    body, status = response_body(text=PRIVATE), 200
    if case == "invalid_schema":
        body = response_body(text=json.dumps(dict(answer=PRIVATE)))
    elif case == "refusal":
        body = response_body(content=[dict(type="refusal", refusal=PRIVATE)])
    elif case == "incomplete":
        body = response_body(text="{", status="incomplete")
    elif case == "provider_error":
        body, status = dict(error=dict(message=PRIVATE, type="server_error")), 503
    async def scenario():
        source = FakeSource()
        async with sdk_adapter([body], http_status=status) as (generator, calls):
            result = await ask_api(generator, decision, source)
        assert result.status_code == 503 and result.json()["status"] == "service_unavailable"
        assert not result.json()["claims"] and not result.json()["citations"]
        assert PRIVATE not in result.text and len(calls) == 1 and source.calls == []
    asyncio.run(scenario())


def test_gate_rejection_makes_zero_real_sdk_http_requests():
    decision, _ = ready()
    decision = decision.model_copy(update={"outcome": "insufficient_evidence"})
    async def scenario():
        source = FakeSource()
        async with sdk_adapter([]) as (generator, calls):
            result = await ask_api(generator, decision, source)
        assert result.status_code == 200 and result.json()["status"] == "insufficient_evidence"
        assert calls == [] and source.calls == []
    asyncio.run(scenario())
