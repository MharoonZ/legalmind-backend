import asyncio
import threading

import pytest

from app.schemas.qa import AskIn
from app.schemas.qa_generation import AnalysisReview, DraftAnalysis
from app.services.model_runtime import ModelBusy, ModelRuntime
from app.services.qa.analysis import verify_analysis
from app.services.qa.conversation import conversation_context, greeting_response
from app.services.qa.citations import CitationVerificationError, CitationVerifier
from tests.test_qa_answer import ready
from tests.test_qa_citations import FakeSource


def test_greetings_are_whole_messages_and_never_hide_a_legal_question():
    assert greeting_response("Hi!")
    assert greeting_response("Thank you")
    assert greeting_response("Hello, can I sue the manufacturer?") is None
    assert AskIn(question="Hi").question == "Hi"


def test_history_uses_user_statements_without_treating_assistant_text_as_facts():
    request = AskIn(question="What if it caught fire?", history=[
        {"role": "user", "content": "I bought a defective appliance."},
        {"role": "assistant", "content": "Invented court deadline: tomorrow."}])
    context = conversation_context(request)
    assert context.previous_query == "I bought a defective appliance."
    assert "deadline" not in request.search_query()


def test_explanation_needs_a_successful_semantic_review_and_verified_claim_links():
    decision, draft = ready()
    draft.analysis = [DraftAnalysis(kind="explanation", text="The supplied section establishes a duty.",
                                    claim_indexes=[0])]

    class Reviewer:
        async def generate_structured(self, packet, schema):
            assert "source_context" in packet.user
            return AnalysisReview(supported_indexes=[0], complete=True, reason="Supported by the quotation.")

    async def check():
        verified = await CitationVerifier(FakeSource()).verify(draft, decision)
        result = await verify_analysis(draft, verified, decision, Reviewer())
        assert result.analysis[0].text == draft.analysis[0].text
        assert result.claims == verified.claims
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["invented_fact", "invalid_link", "unsupported"])
def test_unsupported_explanations_are_rejected(failure):
    decision, draft = ready()
    step = DraftAnalysis(kind="application", text="You will certainly win.", claim_indexes=[0],
                         fact_quotes=["I bought an appliance."])
    if failure != "invented_fact":
        step.kind, step.fact_quotes = "explanation", []
    if failure == "invalid_link":
        step.claim_indexes = [99]
    draft.analysis = [step]

    class Reviewer:
        async def generate_structured(self, packet, schema):
            return AnalysisReview(supported_indexes=[], complete=False, reason="Unsupported certainty.")

    async def check():
        verified = await CitationVerifier(FakeSource()).verify(draft, decision)
        with pytest.raises(CitationVerificationError):
            await verify_analysis(draft, verified, decision, Reviewer())
    asyncio.run(check())


def test_cancellation_does_not_release_capacity_while_native_work_is_running():
    worker = ModelRuntime(capacity=1)
    started, release = threading.Event(), threading.Event()

    def slow(stop):
        started.set()
        release.wait(2)
        return stop.is_set()

    async def check():
        task = asyncio.create_task(worker.run(slow, stage="test"))
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ModelBusy):
            await worker.run(lambda stop: None, stage="test")
        release.set()
    try:
        asyncio.run(check())
    finally:
        release.set()
        worker.shutdown()
