"""Independent test data checks and Phase C integration regressions.

No DB connection, model, HTTP request, or secret is used here.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.routes.qa import get_question_answer_service
from app.core.qa_config import RerankerSettings
from app.main import create_app
from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa.answer import QuestionAnswerService
from app.services.qa.citations import CitationVerificationError, CitationVerifier
from app.services.qa.context import ContextExpander
from tests.qa_fixtures import prepared_context
from app.services.qa.preparation import QuestionPreparationService
from app.services.qa.prompt import build_prompt
from app.services.qa.reranker import CandidateReranker
from tests.qa_fixtures import FakeScorer
from tests.test_qa_answer import FakeGenerator
from tests.test_qa_citations import FakeSource, setup
from tests.test_qa_context import FakeRepo, expand, graph


CASES = json.loads((Path(__file__).parent / "phase_c_corpus_evaluation.json").read_text(encoding="utf-8"))


def test_evaluation_inventory_has_reviewable_source_and_expected_outcomes():
    assert [case["id"] for case in CASES] == [f"E{i:02d}" for i in range(1, 21)]
    assert len({case["question"] for case in CASES}) == 20
    assert [sum(case["category"] == group for case in CASES) for group in
            ("straightforward", "condition/exception", "multiple provisions",
             "missing user fact", "unsupported")] == [5, 4, 1, 3, 3]
    assert all(case["publication_name"] == "legalmind-pilot-20261016" for case in CASES)
    assert all(case["expected_status"] in
               {"answered", "clarification_needed", "insufficient_evidence"} for case in CASES)
    assert all(case["ground_truth_review_status"] for case in CASES)
    for case in CASES:
        if case["id"] not in {"E14", "E20"}:
            assert case["legal_version_id"] and case["revision_id"]
            assert len(case["source_pdf_sha256"]) == 64
        assert len(case["expected_provisions"]) == len({p["provision_id"]
                                                         for p in case["expected_provisions"]})
    assert [(p["label"], p["page_index"]) for p in CASES[3]["expected_provisions"]] == [("10", 5)]
    assert [(p["label"], p["page_index"]) for p in CASES[5]["expected_provisions"]] == [("27", 10)]
    assert [(p["label"], p["page_index"]) for p in CASES[9]["expected_provisions"]] == [("41", 12)]


def test_changed_modal_word_in_a_quoted_rule_is_rejected():
    decision, draft = setup()
    original = decision.context.units[0]
    source_words = "The prison officer shall issue a receipt to every purchaser."
    altered = original.model_copy(update={"text": source_words})
    context = decision.context.model_copy(update={"units": (altered, *decision.context.units[1:])})
    decision = decision.model_copy(update={"context": context})
    false_quote = source_words.replace("shall", "may")
    draft = AnswerDraft(outcome="answered", answer="Untrusted prose",
                        claims=[DraftClaim(evidence_id=altered.evidence_id,
                                           quote=false_quote, claim_text=false_quote)],
                        missing_support="")
    with pytest.raises(CitationVerificationError, match="quotation_mismatch"):
        asyncio.run(CitationVerifier(FakeSource()).verify(draft, decision))


def test_whitespace_only_quote_variation_is_allowed():
    decision, _ = setup()
    original = decision.context.units[0]
    source_words = "The prison officer shall\nissue a written receipt to every purchaser."
    altered = original.model_copy(update={"text": source_words})
    context = decision.context.model_copy(update={"units": (altered, *decision.context.units[1:])})
    decision = decision.model_copy(update={"context": context})
    quote = source_words.replace("\n", " ")
    draft = AnswerDraft(outcome="answered", answer="", claims=[DraftClaim(
        evidence_id=altered.evidence_id, quote=quote, claim_text=quote)], missing_support="")
    verified = asyncio.run(CitationVerifier(FakeSource()).verify(draft, decision))
    assert verified.claims[0].text == quote
    assert verified.citations[0].page_index == 1


def test_a_known_but_unsupplied_evidence_id_is_rejected():
    decision, draft = setup()
    assert len(decision.context.units) > 1
    other_id = decision.context.units[1].evidence_id
    limited = decision.model_copy(update={"evidence_ids": (draft.claims[0].evidence_id,)})
    claim = draft.claims[0].model_copy(update={"evidence_id": other_id})
    with pytest.raises(CitationVerificationError, match="invalid_evidence_id"):
        asyncio.run(CitationVerifier(FakeSource()).verify(
            draft.model_copy(update={"claims": [claim]}), limited))


def test_each_of_two_claims_retains_its_own_source_link():
    catalog, reranked = graph(qualifier=True)
    decision = prepared_context(expand(catalog, reranked))
    source = next(unit for unit in decision.context.units if unit.relation == "retrieved")
    qualifier = next(unit for unit in decision.context.units if unit.relation == "qualification")
    draft = AnswerDraft(outcome="answered", answer="", claims=[
        DraftClaim(evidence_id=source.evidence_id, quote=source.text, claim_text=source.text),
        DraftClaim(evidence_id=qualifier.evidence_id, quote=qualifier.text, claim_text=qualifier.text),
    ], missing_support="")
    verified = asyncio.run(CitationVerifier(FakeSource()).verify(draft, decision))
    assert len(verified.claims) == 2
    assert verified.claims[0].citation_indexes != verified.claims[1].citation_indexes
    assert {c.evidence_id for c in verified.citations} == {source.evidence_id, qualifier.evidence_id}


def test_prompt_does_not_import_server_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-never-send")
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic-secret-never-send@localhost/test")
    catalog, reranked = graph()
    packet = build_prompt(prepared_context(expand(catalog, reranked)))
    assert "synthetic-secret-never-send" not in packet.system + packet.user


def test_public_ask_requests_a_missing_fact_with_verified_supporting_condition():
    catalog, reranked = graph(qualifier=True)
    class Search:
        async def search(self, request):
            return reranked.retrieval.model_copy(update={
                "results": [reranked.candidates[0].candidate],
                "query": request.query, "retrieval_query": request.query,
            })

    prepared = QuestionPreparationService(
        Search(), CandidateReranker(RerankerSettings(), scorer=FakeScorer([1.0])),
        ContextExpander(FakeRepo(catalog), token_counter=lambda value: len(value.split())))
    # After the source/relevance gate, the structured answer step identifies
    # the missing fact. Its supporting condition must pass source verification
    # before the public API can ask the question; no static legal rules added.
    rule = catalog.nodes[next(n.id for n in catalog.nodes.values() if n.heading == "Proviso")]
    generator = FakeGenerator([AnswerDraft(
        outcome="clarification_needed", answer="", missing_support="Whether a signed permit is held.",
        clarification_question="Do you have a signed permit?",
        claims=[DraftClaim(evidence_id=f"provision:{rule.id}", quote=rule.own_text, claim_text=rule.own_text)],
    )])
    service = QuestionAnswerService(prepared, generator, CitationVerifier(FakeSource()))
    app = create_app()
    app.dependency_overrides[get_question_answer_service] = lambda: service
    response = TestClient(app).post("/ask", json={
        "question": "May I act under this section without saying whether I have a signed permit?"})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "clarification_needed"
    assert result["clarification_question"] == "Do you have a signed permit?"
    assert result["citations"][0]["quote"] == rule.own_text
    assert result["missing_support"] == "Whether a signed permit is held."
    assert len(generator.calls) == 1
