"""No live provider calls; these tests exercise the actual generation adapter."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.qa_generation_config import AnswerSettings
from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa_generation import AnswerGenerator, GenerationInvalid, GenerationUnavailable
from app.services.qa_prompt import PromptPacket


PACKET = PromptPacket(system="Use evidence only.", user="Synthetic question and passage.",
                      evidence_ids=("chunk:synthetic",))


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def test_structured_draft_and_server_only_settings():
    draft = AnswerDraft(outcome="partial", answer="A possible lead.",
                        claims=[], missing_support="Other law is absent.")
    responses = FakeResponses(SimpleNamespace(status="completed", output_parsed=draft))
    generator = AnswerGenerator(AnswerSettings(_env_file=None, openai_api_key=None),
                                client=SimpleNamespace(responses=responses))
    assert asyncio.run(generator.generate(PACKET)) == draft
    call = responses.calls[0]
    assert call["text_format"] is AnswerDraft and call["store"] is False
    assert call["input"][0]["role"] == "system"
    assert call["input"][1]["content"] == PACKET.user


def test_missing_key_is_service_failure_before_provider_call():
    generator = AnswerGenerator(AnswerSettings(_env_file=None, openai_api_key=None))
    with pytest.raises(GenerationUnavailable, match="OPENAI_API_KEY"):
        asyncio.run(generator.generate(PACKET))


@pytest.mark.parametrize("response", [SimpleNamespace(status="incomplete", output_parsed=None),
                                      SimpleNamespace(status="completed", output_parsed=None),
                                      SimpleNamespace(status="completed", output_parsed={"answer": "bad"})])
def test_incomplete_refusal_or_bad_json_is_not_an_answer(response):
    generator = AnswerGenerator(AnswerSettings(_env_file=None),
                                client=SimpleNamespace(responses=FakeResponses(response)))
    with pytest.raises(GenerationInvalid):
        asyncio.run(generator.generate(PACKET))


@pytest.mark.parametrize("error_name", ["APITimeoutError", "RateLimitError", "AuthenticationError"])
def test_provider_failures_are_not_reported_as_insufficient_law(error_name):
    error = type(error_name, (Exception,), {})("private SDK detail")
    generator = AnswerGenerator(AnswerSettings(_env_file=None),
                                client=SimpleNamespace(responses=FakeResponses(error=error)))
    with pytest.raises(GenerationUnavailable) as failure:
        asyncio.run(generator.generate(PACKET))
    assert "private SDK detail" not in str(failure.value)


def test_repair_feedback_is_bounded_server_instruction_not_a_new_evidence_item():
    responses = FakeResponses(SimpleNamespace(status="completed", output_parsed=AnswerDraft(
        outcome="insufficient_evidence", answer="", claims=[], missing_support="No quote.")))
    generator = AnswerGenerator(AnswerSettings(_env_file=None),
                                client=SimpleNamespace(responses=responses))
    asyncio.run(generator.generate(PACKET, repair_feedback="quotation_mismatch"))
    assert "quotation_mismatch" in responses.calls[0]["input"][1]["content"]
    assert responses.calls[0]["input"][0]["content"] == PACKET.system


def test_structured_adapter_accepts_a_nullable_followup_question():
    draft = AnswerDraft(outcome="clarification_needed", answer="",
                        missing_support="Whether the user holds a signed permit.",
                        clarification_question="Do you have a signed permit?",
                        claims=[DraftClaim(evidence_id="chunk:synthetic",
                            quote="Provided that a signed permit is required.",
                            claim_text="Provided that a signed permit is required.")])
    responses = FakeResponses(SimpleNamespace(status="completed", output_parsed=draft))
    generator = AnswerGenerator(AnswerSettings(_env_file=None, openai_api_key=None),
                                client=SimpleNamespace(responses=responses))
    assert asyncio.run(generator.generate(PACKET)) == draft
    # Exercise the installed SDK's schema conversion without a network call.
    from openai.lib._pydantic import to_strict_json_schema
    schema = to_strict_json_schema(AnswerDraft)
    assert "clarification_question" in schema["required"]
    assert {item["type"] for item in schema["properties"]["clarification_question"]["anyOf"]} == {"string", "null"}
