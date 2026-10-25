"""End-to-end Phase C composition with synthetic source and fake LLM only."""
import asyncio

from fastapi.testclient import TestClient

from app.api.routes.qa import get_question_answer_service
from app.main import create_app
from app.schemas.qa import AskIn, AskOut
from app.schemas.legal_search import SearchContextIn
from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa_answer import QuestionAnswerService
from app.services.qa_citations import CitationVerifier
from app.services.qa_evidence_gate import EvidenceGate
from app.services.qa_generation import GenerationUnavailable
from tests.test_qa_citations import FakeSource
from tests.test_qa_context import expand, graph


class FakePrep:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def prepare(self, request):
        self.calls.append(request)
        return self.decision


class FakeGenerator:
    def __init__(self, drafts):
        self.drafts = drafts
        self.calls = []

    async def generate(self, packet, *, repair_feedback=None):
        self.calls.append((packet, repair_feedback))
        value = self.drafts[len(self.calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value


def ready():
    catalog, reranked = graph()
    decision = EvidenceGate().decide(expand(catalog, reranked))
    unit = decision.context.units[0]
    draft = AnswerDraft(outcome="answered", answer="The model invented a separate conclusion.",
                        claims=[DraftClaim(evidence_id=unit.evidence_id,
                                           quote=unit.text, claim_text=unit.text)],
                        missing_support="")
    return decision, draft


def answer(decision, drafts, repairs=1):
    preparation = FakePrep(decision)
    generator = FakeGenerator(drafts)
    service = QuestionAnswerService(preparation, generator,
                                    CitationVerifier(FakeSource()), repair_attempts=repairs)
    result = asyncio.run(service.ask(AskIn(question="What does the synthetic section say?")))
    return result, preparation, generator


def test_verified_answer_displays_only_quote_and_citation():
    decision, draft = ready()
    result, prep, generator = answer(decision, [draft])
    assert result.status == "answered"
    assert draft.claims[0].quote in result.answer
    assert "invented a separate conclusion" not in result.answer
    assert result.citations[0].evidence_id == draft.claims[0].evidence_id
    assert len(generator.calls) == len(prep.calls) == 1


def test_partial_answer_is_open_about_uncertainty():
    decision, draft = ready()
    result, _, _ = answer(decision, [draft.model_copy(update={"outcome": "partial",
        "missing_support": "The location and permit status are missing."})])
    assert result.status == "partial"
    assert "can't confirm a complete answer" in result.answer
    assert result.citations
    assert result.missing_support == "The location and permit status are missing."
    assert any(result.missing_support in note for note in result.limitations)


def test_missing_fact_returns_verified_rule_and_a_followup_question():
    catalog, reranked = graph(qualifier=True)
    decision = EvidenceGate().decide(expand(catalog, reranked))
    rule = next(unit for unit in decision.context.units if unit.relation == "qualification")
    draft = AnswerDraft(outcome="clarification_needed", answer="Untrusted conclusion.",
                        missing_support="Whether the user has a signed permit.",
                        clarification_question="Do you have a signed permit?",
                        claims=[DraftClaim(evidence_id=rule.evidence_id, quote=rule.text,
                                           claim_text=rule.text)])
    result, _, generator = answer(decision, [draft])
    assert result.status == "clarification_needed"
    assert result.clarification_question == draft.clarification_question
    assert result.missing_support == draft.missing_support
    assert result.citations and result.citations[0].evidence_id == rule.evidence_id
    assert "Untrusted conclusion" not in result.answer
    assert len(generator.calls) == 1


def test_followup_with_fabricated_support_is_not_returned():
    decision, draft = ready()
    draft = draft.model_copy(update={"outcome": "clarification_needed",
        "clarification_question": "Do you have a signed permit?", "missing_support": "Permit status.",
        "claims": [draft.claims[0].model_copy(update={"evidence_id": "chunk:invented"})]})
    result, _, _ = answer(decision, [draft, draft])
    assert result.status == "insufficient_evidence"
    assert result.clarification_question is None
    assert not result.citations


def test_information_request_does_not_force_a_personal_fact_question():
    decision, draft = ready()
    result, _, _ = answer(decision, [draft])
    assert result.status == "answered" and result.clarification_question is None


def test_gate_rejection_makes_zero_generation_calls():
    decision, draft = ready()
    result, _, generator = answer(decision.model_copy(update={"outcome": "insufficient_evidence"}), [draft])
    assert result.status == "insufficient_evidence"
    assert not generator.calls and not result.citations
    assert "does not mean the law is silent" in result.limitations[0]


def test_provider_failure_is_not_a_false_insufficient_evidence_result():
    decision, _ = ready()
    result, _, _ = answer(decision, [GenerationUnavailable("private")])
    assert result.status == "service_unavailable"
    assert "private" not in result.answer


def test_invalid_citation_gets_one_bounded_repair():
    decision, draft = ready()
    bad = draft.model_copy(update={"claims": [draft.claims[0].model_copy(
        update={"evidence_id": "chunk:invented"})]})
    result, _, generator = answer(decision, [bad, draft])
    assert result.status == "answered"
    assert len(generator.calls) == 2
    assert generator.calls[1][1] == "invalid_evidence_id"


def test_repair_limit_returns_limitation_instead_of_unsupported_answer():
    decision, draft = ready()
    bad = draft.model_copy(update={"claims": [draft.claims[0].model_copy(
        update={"evidence_id": "chunk:invented"})]})
    result, _, generator = answer(decision, [bad, bad])
    assert result.status == "insufficient_evidence"
    assert len(generator.calls) == 2 and not result.citations


def test_ask_api_exposes_contract_and_503_for_service_failure():
    class Unavailable:
        async def ask(self, request):
            return AskOut(status="service_unavailable", question=request.question,
                          answer="Temporarily unavailable.")

    app = create_app()
    app.dependency_overrides[get_question_answer_service] = lambda: Unavailable()
    client = TestClient(app)
    assert "/ask" in app.openapi()["paths"]
    response = client.post("/ask", json={"question": "What does Section 19 say?"})
    assert response.status_code == 503
    assert response.json()["status"] == "service_unavailable"


def test_plain_fact_reply_keeps_the_explicit_previous_question_and_law_scope():
    from tests.qa_fixtures import INSTRUMENT
    decision, draft = ready()
    preparation = FakePrep(decision)
    service = QuestionAnswerService(preparation, FakeGenerator([draft]), CitationVerifier(FakeSource()))
    request = AskIn(question="Yes, I have a signed permit.", context=SearchContextIn(
        previous_query="May I act under this section?", law="Synthetic Test Act", instrument_id=INSTRUMENT))
    result = asyncio.run(service.ask(request))
    search = preparation.calls[0]
    assert search.query == "May I act under this section?\nFollow-up information: Yes, I have a signed permit."
    assert search.law == "Synthetic Test Act" and search.instrument_id == INSTRUMENT
    assert search.context is None  # Already consumed exactly once by Phase C.
    assert result.question == request.question


def test_empty_law_and_overlong_followup_are_input_errors_not_service_failures():
    app = create_app()
    app.dependency_overrides[get_question_answer_service] = lambda: None
    client = TestClient(app)
    assert client.post("/ask", json={"question": "What does the rule say?", "law": " "}).status_code == 422
    response = client.post("/ask", json={"question": "A" * 3000,
        "context": {"previous_query": "B" * 2000}})
    assert response.status_code == 422
