"""Step 13 uses synthetic evidence; it makes no external LLM call."""
import asyncio
from types import SimpleNamespace as Row

from app.core.qa_config import RerankerSettings
from app.schemas.legal_search import LegalSearchIn, LegalSearchOut
from app.schemas.qa_context import ContextIssue
from app.schemas.qa_evidence import RerankedCandidate
from app.schemas.qa_decision import NecessaryFact
from app.services.legal_retrieval import LegalRetrievalService
from app.services.qa_context import ContextExpander
from app.services.qa_evidence_gate import EvidenceGate
from app.services.qa_preparation import QuestionPreparationService
from app.services.qa_reranker import CandidateReranker
from tests.qa_fixtures import FakeScorer, PUBLICATION, candidate, retrieval
from tests.test_qa_context import FakeRepo, expand, graph


def test_exact_published_reference_can_go_to_model_review_with_one_complete_passage():
    catalog, reranked = graph(qualifier=True)
    source = reranked.candidates[0].candidate.model_copy(update={"exact_rank": 1})
    reranked = reranked.model_copy(update={"candidates": (reranked.candidates[0].model_copy(
        update={"candidate": source}),)})
    context = expand(catalog, reranked)
    outcome = EvidenceGate().decide(context)
    assert outcome.outcome == "ready_for_prompt"
    assert outcome.context is context
    assert any(unit.relation == "qualification" for unit in context.units)
    assert outcome.evidence_ids


def test_conceptual_topic_overlap_is_disclosed_as_unvalidated_heuristic():
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    outcome = EvidenceGate().decide(context)
    assert outcome.outcome == "ready_for_prompt"
    assert any("unvalidated" in note for note in outcome.heuristic_notes)


def test_unrelated_question_is_insufficient_with_only_a_high_reranker_score():
    catalog, reranked = graph()
    row = reranked.candidates[0]
    reranked = reranked.model_copy(update={"retrieval": reranked.retrieval.model_copy(
        update={"query": "astronaut pizza on Mars"}), "candidates": (
            row.model_copy(update={"reranker_score": 100, "candidate": row.candidate.model_copy(
                update={"vector_rank": None, "vector_score": None})}),)})
    outcome = EvidenceGate().decide(expand(catalog, reranked))
    assert outcome.outcome == "insufficient_evidence"


def test_paraphrase_can_reach_grounded_review_using_both_semantic_signals():
    catalog, reranked = graph()
    source_text = "A shopkeeper shall provide a receipt to each purchaser."
    catalog.chunks[reranked.candidates[0].candidate.chunk_id].chunk_text = source_text
    catalog.nodes[reranked.candidates[0].candidate.root_provision_id].own_text = source_text
    row = reranked.candidates[0]
    source = row.candidate.model_copy(update={"chunk_text": source_text})
    reranked = reranked.model_copy(update={
        "retrieval": reranked.retrieval.model_copy(update={
            "query": "The seller refused to give me proof of purchase", "results": [source]}),
        "candidates": (row.model_copy(update={"candidate": source}),),
    })
    result = EvidenceGate().decide(expand(catalog, reranked))
    assert result.outcome == "ready_for_prompt"
    assert "semantic" in result.reason
    assert any("does not prove" in note for note in result.heuristic_notes)


def test_dense_candidate_without_positive_reranker_match_cannot_admit_unrelated_text():
    catalog, reranked = graph()
    reranked = reranked.model_copy(update={
        "retrieval": reranked.retrieval.model_copy(update={"query": "astronaut pizza on Mars"}),
        "candidates": (reranked.candidates[0].model_copy(update={"reranker_score": -4}),),
    })
    assert EvidenceGate().decide(expand(catalog, reranked)).outcome == "insufficient_evidence"


def test_lexical_search_still_works_without_semantic_vectors():
    catalog, reranked = graph()
    row = reranked.candidates[0]
    reranked = reranked.model_copy(update={"candidates": (
        row.model_copy(update={"candidate": row.candidate.model_copy(
            update={"vector_rank": None, "vector_score": None})}),)})
    decision = EvidenceGate().decide(expand(catalog, reranked))
    assert decision.outcome == "ready_for_prompt"
    assert "topic overlap" in decision.reason


def test_missing_required_qualification_blocks_generation():
    catalog, reranked = graph(qualifier=True)
    catalog.missing.add(next(n.id for n in catalog.nodes.values() if n.heading == "Proviso"))
    outcome = EvidenceGate().decide(expand(catalog, reranked))
    assert outcome.outcome == "insufficient_evidence"


def test_necessary_fact_clarification_only_when_rule_evidence_is_present():
    catalog, reranked = graph(qualifier=True)
    context = expand(catalog, reranked)
    proviso = next(unit for unit in context.units if unit.relation == "qualification")
    fact = NecessaryFact(key="signed_permit", question="Do you have a signed permit?",
                         evidence_ids=(proviso.evidence_id,), quote=proviso.text)
    missing = EvidenceGate().decide(context, necessary_facts=(fact,))
    assert missing.outcome == "clarification_needed"
    assert missing.clarification_question == fact.question
    known = EvidenceGate().decide(context, necessary_facts=(fact,),
                                  known_fact_keys=frozenset({"signed_permit"}))
    assert known.outcome == "ready_for_prompt"
    unsupported = EvidenceGate().decide(context, necessary_facts=(fact.model_copy(
        update={"evidence_ids": ("provision:not-in-publication",)}),))
    assert unsupported.outcome == "insufficient_evidence"


def test_no_results_is_not_clarification_and_no_publication_is_service_failure():
    catalog, reranked = graph()
    for status, expected in [("no_results", "insufficient_evidence"),
                             ("no_active_publication", "service_unavailable")]:
        empty = reranked.model_copy(update={"retrieval": retrieval([], status=status), "candidates": ()})
        context = expand(catalog, empty)
        assert EvidenceGate().decide(context).outcome == expected


def test_upstream_ambiguity_requests_clarification_without_models_or_db():
    catalog, reranked = graph()
    ambiguous = reranked.model_copy(update={"retrieval": retrieval([], status="needs_clarification",
        message="Specify the law.", clarification_options=[{"instrument_id": "synthetic"}]),
        "candidates": ()})
    context = expand(catalog, ambiguous)
    outcome = EvidenceGate().decide(context)
    assert outcome.outcome == "clarification_needed"
    assert outcome.clarification_question == "Specify the law."


def test_preparation_composes_real_retrieval_rrf_with_rerank_context_and_gate():
    catalog, _ = graph()
    source = candidate(1)
    class SearchRepository:
        calls = 0
        async def active_catalog(self):
            return {"id": PUBLICATION, "name": "synthetic"}, [{
                "instrument_id": source.instrument_id, "canonical_title": source.instrument_title,
                "citation_key": source.citation_key, "aliases": [],
                "jurisdiction_code": source.jurisdiction_code, "edition_key": source.edition_key}]
        async def retrieve(self, publication_id, **kwargs):
            self.calls += 1
            assert publication_id == PUBLICATION
            return [], [], [source.model_dump()]
    async def encode(texts, profile, **kwargs):
        return [[1.0] + [0.0] * 1023]
    search_repo = SearchRepository()
    settings = Row(legal_search_candidate_pool=50, legal_search_min_similarity=.48,
                   legal_search_hnsw_ef_search=200, legal_search_rrf_k=60)
    scorer = FakeScorer([2.5])
    context_repo = FakeRepo(catalog)
    prepared = QuestionPreparationService(
        LegalRetrievalService(search_repo, settings, encode=encode),
        CandidateReranker(RerankerSettings(), scorer=scorer),
        ContextExpander(context_repo, token_counter=lambda text: len(text.split())),
        EvidenceGate(),
    )
    decision = asyncio.run(prepared.prepare(LegalSearchIn(query="What does synthetic source say?")))
    assert decision.outcome == "ready_for_prompt"
    assert search_repo.calls == 1 and len(scorer.calls) == 1
    assert context_repo.calls == [PUBLICATION]
    assert decision.context.reranked.candidates[0].candidate.rrf_score > 0
    assert decision.context.reranked.candidates[0].reranker_score == 2.5


def test_rejected_preparation_returns_before_any_answer_generator_exists():
    class EmptyRetrieval:
        calls = 0
        async def search(self, request):
            self.calls += 1
            return LegalSearchOut(status="no_results", query=request.query,
                                  retrieval_query=request.query)
    scorer = FakeScorer(error=AssertionError("model should not load"))
    catalog, _ = graph()
    repo = FakeRepo(catalog)
    search = EmptyRetrieval()
    prepared = QuestionPreparationService(
        search, CandidateReranker(RerankerSettings(), scorer=scorer),
        ContextExpander(repo, token_counter=lambda text: len(text.split())), EvidenceGate())
    result = asyncio.run(prepared.prepare(LegalSearchIn(query="unsupported topic")))
    assert result.outcome == "insufficient_evidence"
    assert search.calls == 1 and scorer.calls == [] and repo.calls == []
    # Steps 14–17 are not wired; no answer-generation client is constructed here.


def test_model_failure_is_service_error_not_insufficient_law():
    class FoundRetrieval:
        async def search(self, request):
            return retrieval([candidate(1)])
    catalog, _ = graph()
    prepared = QuestionPreparationService(
        FoundRetrieval(), CandidateReranker(RerankerSettings(), scorer=FakeScorer(error=RuntimeError())),
        ContextExpander(FakeRepo(catalog), token_counter=lambda text: len(text.split())),
        EvidenceGate())
    result = asyncio.run(prepared.prepare(LegalSearchIn(query="synthetic source")))
    assert result.outcome == "service_unavailable"
    assert result.context is None


def test_partial_context_retries_one_relevant_root_and_keeps_its_required_source():
    catalog, original = graph()
    first = original.candidates[0]
    second = RerankedCandidate(publication_id=PUBLICATION, candidate=candidate(2),
                               reranker_rank=2, reranker_score=1)
    response = retrieval([first.candidate, second.candidate], query="When are isolated source words considered?")
    ranked = original.model_copy(update={"retrieval": response, "candidates": (first, second)})
    complete = expand(catalog, original)

    class Search:
        async def search(self, request):
            return response

    class Rerank:
        async def rerank(self, response):
            return ranked

    class Expand:
        async def expand(self, result):
            issues = (ContextIssue(code="unresolved_reference", detail="Second root is incomplete.",
                                   critical=True),) if len(result.candidates) > 1 else ()
            return complete.model_copy(update={"reranked": result, "issues": issues})

    service = QuestionPreparationService(Search(), Rerank(), Expand(), EvidenceGate())
    decision = asyncio.run(service.prepare(LegalSearchIn(query=response.query)))
    assert decision.outcome == "ready_for_prompt" and decision.limited_source_context
    assert decision.context.reranked.candidates == (first,)
