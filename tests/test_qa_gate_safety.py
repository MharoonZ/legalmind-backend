"""Pre-generation gate decisions and public no-AI follow-ups, using fake sources."""
import asyncio

from fastapi.testclient import TestClient
import pytest

from app.core.config import get_settings
from app.main import create_app
from app.schemas.qa import AskIn
from app.schemas.qa_decision import FactAssessment, NecessaryFact
from app.schemas.qa_evidence import RerankResult, RerankedCandidate
from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa_answer import QuestionAnswerService
from app.services.qa_citations import CitationVerificationError, CitationVerifier
from app.services.qa_evidence_gate import EvidenceGate
from app.services.qa_preparation import QuestionPreparationService
from tests.qa_fixtures import FakeScorer, PUBLICATION, candidate, identity, retrieval
from tests.test_qa_answer import FakeGenerator, FakePrep
from tests.test_qa_citations import FakeSource
from tests.test_qa_context import FakeRepo, expand, graph


def supported_fact(context):
    unit = next(u for u in context.units if u.relation == "qualification")
    return NecessaryFact(key="signed_permit", question="Do you have a signed permit?",
                         evidence_ids=(unit.evidence_id,), quote=unit.text)


def lexical_context(sources, *, query="payroll notice"):
    catalog, original = graph()
    base = expand(catalog, original)
    unit = base.units[0]
    units = tuple(unit.model_copy(update={
        "evidence_id": f"chunk:{s.chunk_id}", "chunk_id": s.chunk_id,
        "provision_id": s.root_provision_id, "text": s.chunk_text,
        "source_refs": s.source_refs, "instrument_id": s.instrument_id,
        "legal_version_id": s.legal_version_id, "revision_id": s.revision_id,
        "chunk_set_id": s.chunk_set_id,
    }) for s in sources)
    reranked = RerankResult(retrieval=retrieval(sources, query=query, retrieval_query=query),
        model_name="synthetic", candidates=tuple(RerankedCandidate(publication_id=PUBLICATION,
            candidate=s, reranker_score=-1, reranker_rank=i) for i, s in enumerate(sources, 1)))
    return base.model_copy(update={"units": units, "reranked": reranked})


@pytest.mark.parametrize("fault", ["span", "file", "block", "hash", "page_zero", "page_boolean", "approximate"])
def test_source_identity_is_checked_before_ai_eligibility(fault):
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    unit = context.units[0]
    ref = dict(unit.source_refs[0])
    if fault in {"span", "file", "block"}:
        ref[{"span": "source_span_id", "file": "file_id", "block": "block_id"}[fault]] = "missing-id"
    elif fault == "hash":
        ref["file_sha256"] = "not-a-hash"
    elif fault.startswith("page"):
        ref["page_index"] = False if fault == "page_boolean" else 0
    else:
        ref["mapping_quality"] = "approximate"
    context = context.model_copy(update={"units": (unit.model_copy(update={"source_refs": [ref]}), *context.units[1:])})
    assert EvidenceGate().decide(context).outcome == "insufficient_evidence"


@pytest.mark.parametrize("fault", ["publication", "version", "revision", "chunk_set", "text", "root", "evidence_id", "duplicate"])
def test_context_identity_cannot_be_detached_from_retained_candidates(fault):
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    unit = context.units[0]
    fields = {"publication": "publication_id", "version": "legal_version_id", "revision": "revision_id",
              "chunk_set": "chunk_set_id", "root": "provision_id"}
    if fault in fields:
        changed = unit.model_copy(update={fields[fault]: identity("wrong-identity")})
    elif fault == "text":
        changed = unit.model_copy(update={"text": "A different unselected source passage."})
    elif fault == "evidence_id":
        changed = unit.model_copy(update={"evidence_id": "chunk:invented"})
    else:
        changed = unit
    units = (changed, *context.units[1:]) + ((unit,) if fault == "duplicate" else ())
    assert EvidenceGate().decide(context.model_copy(update={"units": units})).outcome == "insufficient_evidence"


def test_conflicting_version_in_a_supplemental_passage_is_not_ignored():
    catalog, reranked = graph(qualifier=True)
    context = expand(catalog, reranked)
    units = tuple(u.model_copy(update={"revision_id": identity("other-version")})
                  if u.relation == "qualification" else u for u in context.units)
    assert EvidenceGate().decide(context.model_copy(update={"units": units})).outcome == "insufficient_evidence"


def test_unretained_exact_hit_cannot_approve_a_different_retained_passage():
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    extra = RerankedCandidate(publication_id=PUBLICATION, candidate=candidate(2, exact_rank=1),
                              reranker_score=10, reranker_rank=2)
    changed = context.reranked.model_copy(update={"candidates": (*context.reranked.candidates, extra)})
    assert EvidenceGate().decide(context.model_copy(update={"reranked": changed})).outcome == "insufficient_evidence"


def test_zero_exact_rank_does_not_bypass_the_source_contract():
    source = candidate(1, exact_rank=0)
    assert EvidenceGate().decide(lexical_context([source])).outcome == "insufficient_evidence"


def test_topic_words_from_unrelated_sections_cannot_be_pooled_to_pass():
    first = candidate(1, chunk_text="The payroll procedure applies.", vector_rank=None, vector_score=None)
    second = candidate(2, chunk_text="A notice must be delivered.", vector_rank=None, vector_score=None)
    assert EvidenceGate().decide(lexical_context([first, second])).outcome == "insufficient_evidence"


def test_fragments_of_the_same_section_can_jointly_establish_topic_overlap():
    first = candidate(1, chunk_text="The payroll procedure applies.", vector_rank=None, vector_score=None,
                      root_total_chunks=2)
    second = candidate(2, chunk_text="A notice must be delivered.", vector_rank=None, vector_score=None,
                       root_provision_id=first.root_provision_id, sequence_in_root=2, root_total_chunks=2)
    assert EvidenceGate().decide(lexical_context([first, second])).outcome == "ready_for_prompt"


def test_missing_fact_does_not_trigger_a_personal_question_for_an_unrelated_query():
    catalog, reranked = graph(qualifier=True)
    row = reranked.candidates[0]
    source = row.candidate.model_copy(update={"vector_rank": None, "vector_score": None})
    reranked = reranked.model_copy(update={"candidates": (row.model_copy(update={"candidate": source}),),
        "retrieval": reranked.retrieval.model_copy(update={"query": "astronaut pizza on Mars"})})
    context = expand(catalog, reranked)
    decision = EvidenceGate().decide(context, necessary_facts=(supported_fact(context),))
    assert decision.outcome == "insufficient_evidence" and decision.clarification_question is None


@pytest.mark.parametrize("fault", ["quote", "missing_id", "known_but_missing_id", "duplicate_key"])
def test_fact_questions_require_available_exact_condition_text(fault):
    catalog, reranked = graph(qualifier=True)
    context = expand(catalog, reranked)
    fact = supported_fact(context)
    known = frozenset()
    if fault == "quote":
        fact = fact.model_copy(update={"quote": "An invented condition that was never in the source."})
    elif "missing_id" in fault:
        fact = fact.model_copy(update={"evidence_ids": ("provision:omitted",)})
        if fault.startswith("known"):
            known = frozenset({fact.key})
    facts = (fact, fact) if fault == "duplicate_key" else (fact,)
    assert EvidenceGate().decide(context, necessary_facts=facts, known_fact_keys=known).outcome == "insufficient_evidence"


def test_optional_definition_omission_alone_does_not_block_good_evidence():
    catalog, reranked = graph(definition=True)
    catalog.references[identity("definitions-seven")] = [type("Ref", (), {"resolution_status": "unresolved"})()]
    context = expand(catalog, reranked)
    assert any(i.code == "optional_context_omitted" for i in context.issues)
    assert EvidenceGate().decide(context).outcome == "ready_for_prompt"


def fact_decision():
    catalog, reranked = graph(qualifier=True)
    context = expand(catalog, reranked)
    fact = supported_fact(context)
    return EvidenceGate().decide(context, necessary_facts=(fact,))


def test_gate_fact_followup_has_verified_citation_and_zero_generation_calls():
    decision = fact_decision()
    source, generator = FakeSource(), FakeGenerator([])
    service = QuestionAnswerService(FakePrep(decision), generator, CitationVerifier(source))
    result = asyncio.run(service.ask(AskIn(question="May I act under this section?")))
    assert result.status == "clarification_needed"
    assert result.clarification_question == "Do you have a signed permit?"
    assert len(result.claims) == 1 and result.citations
    assert len(source.calls) == 1 and generator.calls == []


@pytest.mark.parametrize("code,expected", [("source_text_changed", "insufficient_evidence"),
                                           ("source_span_missing", "insufficient_evidence"),
                                           ("source_verification_unavailable", "service_unavailable")])
def test_failed_fresh_fact_source_check_never_returns_an_unsupported_followup(code, expected):
    class FailedSource(FakeSource):
        async def verify_many(self, requests):
            raise CitationVerificationError(code)
    generator = FakeGenerator([])
    service = QuestionAnswerService(FakePrep(fact_decision()), generator, CitationVerifier(FailedSource()))
    result = asyncio.run(service.ask(AskIn(question="May I act under this section?")))
    assert result.status == expected and result.clarification_question is None
    assert not result.citations and generator.calls == []


@pytest.mark.parametrize("fault", ["question", "quote", "evidence_id", "outcome"])
def test_gate_fact_citation_verifier_binds_the_exact_approved_question_and_condition(fault):
    decision = fact_decision()
    fact = decision.clarification_fact
    claim = DraftClaim(evidence_id=decision.clarification_evidence_id, quote=fact.quote, claim_text=fact.quote)
    draft = AnswerDraft(outcome="clarification_needed", answer="", claims=[claim],
                        clarification_question=fact.question, missing_support=decision.reason)
    if fault == "question":
        draft = draft.model_copy(update={"clarification_question": "Tell me an unrelated private detail?"})
    elif fault == "quote":
        draft = draft.model_copy(update={"claims": [claim.model_copy(update={"quote": "A different source condition."})]})
    elif fault == "evidence_id":
        draft = draft.model_copy(update={"claims": [claim.model_copy(update={"evidence_id": "provision:invented"})]})
    else:
        draft = draft.model_copy(update={"outcome": "answered"})
    with pytest.raises(CitationVerificationError, match="gate_clarification_mismatch"):
        asyncio.run(CitationVerifier(FakeSource()).verify(draft, decision))


class Provider:
    def __init__(self, assessment=None, error=None):
        self.assessment, self.error, self.calls = assessment, error, []
    async def assess(self, request, context):
        self.calls.append((request, context))
        if self.error:
            raise self.error
        return self.assessment


def test_public_api_uses_server_fact_provider_and_cites_followup_without_ai(monkeypatch):
    import app.api.routes.qa as route
    from app.core.qa_config import RerankerSettings
    from app.services.qa_reranker import CandidateReranker
    from app.services.qa_context import ContextExpander
    catalog, reranked = graph(qualifier=True)
    context = expand(catalog, reranked)
    provider = Provider(FactAssessment(necessary_facts=(supported_fact(context),)))
    generator = FakeGenerator([])
    class Search:
        async def search(self, request):
            return reranked.retrieval.model_copy(update={"results": [reranked.candidates[0].candidate],
                                                        "query": request.query, "retrieval_query": request.query})
    monkeypatch.setattr(route, "LegalRetrievalService", lambda *args: Search())
    monkeypatch.setattr(route, "CandidateReranker", lambda: CandidateReranker(RerankerSettings(), scorer=FakeScorer([1])))
    monkeypatch.setattr(route, "ContextExpander", lambda *args: ContextExpander(FakeRepo(catalog), token_counter=lambda text: len(text.split())))
    monkeypatch.setattr(route, "QaSourceVerifier", lambda *args: FakeSource())
    monkeypatch.setattr(route, "_generator", lambda: generator)
    app = create_app()
    app.state.db = object()
    app.state.qa_fact_requirement_provider = provider
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update={"legal_search_enabled": True})
    response = TestClient(app).post("/ask", json={"question": "May I act under this section?"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification_needed" and body["citations"]
    assert body["clarification_question"] == "Do you have a signed permit?"
    assert len(provider.calls) == 1 and generator.calls == []
    provider.assessment = FactAssessment(necessary_facts=(supported_fact(context),),
                                         known_fact_keys=frozenset({"signed_permit"}))
    unit = context.units[0]
    generator.drafts.append(AnswerDraft(outcome="answered", answer="", missing_support="",
        claims=[DraftClaim(evidence_id=unit.evidence_id, quote=unit.text, claim_text=unit.text)]))
    known = TestClient(app).post("/ask", json={"question": "May I act under this section? I have a signed permit."})
    assert known.status_code == 200 and known.json()["status"] == "answered"
    assert known.json()["clarification_question"] is None and len(generator.calls) == 1
    refused = TestClient(app).post("/ask", json={"question": "May I act?", "known_fact_keys": ["signed_permit"]})
    assert refused.status_code == 422


def test_provider_failure_is_service_error_with_zero_generation_calls():
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    class Search:
        async def search(self, request): return reranked.retrieval
    class Rerank:
        async def rerank(self, response): return reranked
    class Expand:
        async def expand(self, response): return context
    provider = Provider(error=RuntimeError("private details"))
    prepared = QuestionPreparationService(Search(), Rerank(), Expand(), EvidenceGate(), fact_provider=provider)
    generator = FakeGenerator([])
    service = QuestionAnswerService(prepared, generator, CitationVerifier(FakeSource()))
    result = asyncio.run(service.ask(AskIn(question="What does this source say?")))
    assert result.status == "service_unavailable" and "private" not in result.answer
    assert generator.calls == []


def test_unusable_context_never_invokes_the_fact_provider_or_ai():
    catalog, reranked = graph(unresolved=True)
    context = expand(catalog, reranked)
    class Search:
        async def search(self, request): return reranked.retrieval
    class Rerank:
        async def rerank(self, response): return reranked
    class Expand:
        async def expand(self, response): return context
    provider = Provider(error=AssertionError("Fact rules must not be evaluated"))
    prepared = QuestionPreparationService(Search(), Rerank(), Expand(), EvidenceGate(), fact_provider=provider)
    generator = FakeGenerator([])
    result = asyncio.run(QuestionAnswerService(prepared, generator, CitationVerifier(FakeSource())).ask(
        AskIn(question="What does this source say?")))
    assert result.status == "insufficient_evidence"
    assert provider.calls == [] and generator.calls == []
