"""Preparation delegates relevance to generation and enforces source completeness."""
import asyncio
from types import SimpleNamespace

import pytest

from app.schemas.legal_search import LegalSearchIn
from app.services.qa.preparation import QuestionPreparationService
from app.services.qa.reranker import RerankerUnavailable
from tests.qa_fixtures import retrieval
from tests.test_qa_context import expand, graph


@pytest.mark.parametrize("status,expected", [
    ("no_results", "insufficient_evidence"), ("needs_clarification", "clarification_needed"),
    ("no_active_publication", "service_unavailable"), ("service_unavailable", "service_unavailable"),
])
def test_non_success_retrieval_returns_before_models(status, expected):
    async def search(request):
        return retrieval([], status=status, message="Specify the law.")

    class Forbidden:
        async def rerank(self, response):
            pytest.fail("A non-successful retrieval must not load the model.")

    result = asyncio.run(QuestionPreparationService(
        SimpleNamespace(search=search), Forbidden(), None).prepare(LegalSearchIn(query="Question")))
    assert result.outcome == expected


def test_negative_reranker_logit_does_not_filter_a_source_complete_candidate():
    catalog, ranked = graph()
    row = ranked.candidates[0].model_copy(update={"reranker_score": -50})
    ranked = ranked.model_copy(update={"candidates": (row,)})
    context = expand(catalog, ranked)

    async def search(request):
        return ranked.retrieval

    async def rerank(response):
        return ranked

    async def build(response):
        return context

    service = QuestionPreparationService(SimpleNamespace(search=search),
        SimpleNamespace(rerank=rerank), SimpleNamespace(expand=build))
    result = asyncio.run(service.prepare(LegalSearchIn(query="A differently worded query")))
    assert result.outcome == "ready_for_prompt" and result.evidence_ids


def test_incomplete_dependency_is_reported_as_missing_context():
    catalog, ranked = graph(unresolved=True)
    context = expand(catalog, ranked)

    async def search(request):
        return ranked.retrieval

    async def rerank(response):
        return ranked

    async def build(response):
        return context

    result = asyncio.run(QuestionPreparationService(SimpleNamespace(search=search),
        SimpleNamespace(rerank=rerank), SimpleNamespace(expand=build)).prepare(LegalSearchIn(query="Question")))
    assert result.outcome == "insufficient_evidence"


def test_reranker_failure_is_not_reported_as_missing_law():
    async def search(request):
        return retrieval()

    async def rerank(response):
        raise RerankerUnavailable("Private provider detail")

    result = asyncio.run(QuestionPreparationService(SimpleNamespace(search=search),
        SimpleNamespace(rerank=rerank), None).prepare(LegalSearchIn(query="Question")))
    assert result.outcome == "service_unavailable" and "Private" not in result.reason
