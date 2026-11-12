import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.core.qa_config import RerankerSettings
from app.schemas.legal_search import LegalSearchIn
from app.services.legal_retrieval import LegalRetrievalService
from app.services.qa_reranker import (
    CandidateReranker, EvidenceContractError, FlagEmbeddingScorer,
    RerankerInputTooLong, RerankerUnavailable, get_flag_scorer,
)
from tests.qa_fixtures import FakeScorer, candidate, identity, retrieval


def run(source=None, *, scores=None, error=None, top_k=5):
    scorer = FakeScorer(scores=scores, error=error)
    result = asyncio.run(CandidateReranker(RerankerSettings(top_k=top_k), scorer=scorer)
                         .rerank(source or retrieval()))
    return result, scorer


def test_order_and_all_upstream_identity_text_metadata_and_scores_are_preserved():
    source = retrieval()
    before = source.model_dump()
    result, scorer = run(source)
    assert [r.candidate.rank for r in result.candidates] == [10, 9, 8, 7, 6]
    assert [r.reranker_rank for r in result.candidates] == [1, 2, 3, 4, 5]
    assert [r.reranker_score for r in result.candidates] == [9, 8, 7, 6, 5]
    assert len(result.all_scores) == 10 and len(result.dropped_chunk_ids) == 5
    for row in result.candidates:
        assert row.candidate.model_dump() == before["results"][row.candidate.rank - 1]
        assert row.publication_id == source.publication_id
    assert scorer.calls[0][0] == (source.retrieval_query, source.results[0].chunk_text)
    result.candidates[0].candidate.source_refs[0]["filename"] = "changed-only-in-copy"
    assert source.model_dump() == before


def test_exact_reference_fragments_keep_their_upstream_order():
    source = retrieval([candidate(1, exact_rank=1), candidate(2, exact_rank=2), candidate(3)])
    result, _ = run(source, scores=[-20, -30, 100])
    assert [r.candidate.rank for r in result.candidates] == [1, 2, 3]
    assert result.candidates[0].reranker_score == -20  # no fabricated score boost


def test_numbered_lookup_bypasses_the_cross_encoder_without_making_up_scores():
    source = retrieval([candidate(1, exact_rank=1), candidate(2, exact_rank=2)])
    result, scorer = run(source, error=RuntimeError("model must stay unloaded"))
    assert [row.candidate.exact_rank for row in result.candidates] == [1, 2]
    assert all(row.reranker_score is None for row in result.candidates)
    assert not result.all_scores and not scorer.calls


def test_tied_scores_have_stable_upstream_order():
    result, _ = run(scores=[1] * 10)
    assert [r.candidate.rank for r in result.candidates] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("status", ["ok", "no_results", "needs_clarification", "no_active_publication"])
def test_empty_or_non_answerable_results_never_load_or_call_the_model(status):
    result, scorer = run(retrieval([], status=status))
    assert result.candidates == () and scorer.calls == []


def test_model_errors_are_explicit_and_do_not_fall_back_to_rrf():
    with pytest.raises(RerankerUnavailable):
        run(error=RuntimeError("simulated model failure"))


@pytest.mark.parametrize("scores", [[1], [float("nan")] * 10, [float("inf")] * 10, [True] * 10])
def test_bad_model_outputs_are_service_failures(scores):
    with pytest.raises(RerankerUnavailable):
        run(scores=scores)


@pytest.mark.parametrize("changes", [
    {"publication_id": None},
    {"results": [candidate(1), candidate(1)]},
    {"results": [candidate(1), candidate(2, revision_id=identity("wrong-version"))]},
    {"status": "no_results"},
])
def test_invalid_upstream_identity_is_rejected_before_model_use(changes):
    source = retrieval(**changes)
    scorer = FakeScorer()
    with pytest.raises(EvidenceContractError):
        asyncio.run(CandidateReranker(scorer=scorer).rerank(source))
    assert scorer.calls == []


@pytest.mark.parametrize("top_k", [5, 8, 10])
def test_top_k_is_configurable(top_k):
    result, _ = run(top_k=top_k)
    assert len(result.candidates) == top_k


@pytest.mark.parametrize("top_k", [4, 11])
def test_top_k_cannot_leave_the_assigned_range(top_k):
    with pytest.raises(ValidationError):
        RerankerSettings(top_k=top_k)


def test_singleton_model_is_reused_across_service_instances():
    settings = RerankerSettings()
    assert CandidateReranker(settings).scorer is CandidateReranker(settings).scorer
    assert get_flag_scorer(settings) is get_flag_scorer(settings)


def test_lazy_loading_and_raw_scores_with_no_silent_token_truncation():
    calls = []
    class Tokenizer:
        def encode(self, question, passage, **kwargs):
            assert kwargs == {"add_special_tokens": True, "truncation": False}
            return list(range(len(question) + len(passage)))
    class Model:
        tokenizer = Tokenizer()
        def compute_score(self, pairs, **kwargs):
            assert kwargs["normalize"] is False
            assert kwargs["query_max_length"] == kwargs["max_length"] == 32
            return -5.0 if len(pairs) == 1 else [-5.0] * len(pairs)
    def loader():
        calls.append("load")
        return Model()
    scorer = FlagEmbeddingScorer(RerankerSettings(max_pair_tokens=32), loader=loader)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _: scorer.score_pairs([("q", "text")]), range(3))) == [[-5.0]] * 3
    assert calls == ["load"]
    with pytest.raises(RerankerInputTooLong):
        scorer.score_pairs([("q" * 20, "text" * 20)])


def test_startup_warmup_loads_the_same_model_used_by_requests_once(monkeypatch):
    from app.services import qa_reranker
    loads = []
    class Model:
        tokenizer = type("Tokenizer", (), {"encode": lambda *a, **k: [1, 2]})()

        def compute_score(self, pairs, **kwargs):
            return [2.0] * len(pairs)
    def loader():
        loads.append(True)
        return Model()
    scorer = FlagEmbeddingScorer(RerankerSettings(), loader=loader)
    monkeypatch.setattr(qa_reranker, "get_flag_scorer", lambda _: scorer)
    assert asyncio.run(qa_reranker.warm_reranker()) is True
    assert asyncio.run(qa_reranker.warm_reranker()) is True
    assert scorer.score_pairs([("question", "passage")]) == [2.0]
    assert len(loads) == 1


def test_failed_or_disabled_warmup_never_fabricates_scores(monkeypatch):
    from app.services import qa_reranker
    def fail():
        raise RuntimeError("simulated model failure")
    scorer = FlagEmbeddingScorer(RerankerSettings(), loader=fail)
    monkeypatch.setattr(qa_reranker, "get_flag_scorer", lambda _: scorer)
    assert asyncio.run(qa_reranker.warm_reranker()) is False
    assert scorer._model is None
    assert asyncio.run(qa_reranker.warm_reranker(RerankerSettings(warm_on_startup=False))) is False
    with pytest.raises(RerankerUnavailable):
        asyncio.run(CandidateReranker(scorer=scorer).rerank(retrieval()))


def test_real_upstream_service_contract_can_feed_reranker_without_recomputing_rrf(settings):
    from tests.test_legal_retrieval import Repository, encode, candidate as upstream_candidate
    repo = Repository(rows=([], [upstream_candidate(keyword_score=.8)], []))
    async def check():
        upstream = await LegalRetrievalService(repo, settings, encode=encode).search(
            LegalSearchIn(query="receipt", limit=10))
        result = await CandidateReranker(scorer=FakeScorer([2.5])).rerank(upstream)
        assert result.candidates[0].candidate.model_dump() == upstream.results[0].model_dump()
        assert result.candidates[0].candidate.rrf_score == upstream.results[0].rrf_score
        assert len(repo.calls) == 1
    asyncio.run(check())
