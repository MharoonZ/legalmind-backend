"""Budgeted reranking and evidence assembly."""
import asyncio

import pytest

from app.core.inference import InferenceRuntime
from app.qa.context import build_context
from app.qa.rerank import CrossEncoderReranker
from app.retrieval.index import PublicationIndex
from app.retrieval.service import QueryEncoder, RetrievalService
from tests.corpus import CONSUMER, GAMBLING, fake_encode, make_snapshot, settings


@pytest.fixture(scope="module")
def snapshot():
    return make_snapshot()


def retrieve(snapshot, question, **overrides):
    service = RetrievalService(PublicationIndex.from_snapshot(snapshot), settings(**overrides),
                               encoder=QueryEncoder(runtime=InferenceRuntime(), encode=fake_encode))
    return asyncio.run(service.retrieve(question))


def rerank(reranker, result, snapshot):
    return asyncio.run(reranker.rerank(result.analysis.search_text, result.candidates, snapshot))


def test_exact_lookups_keep_the_order_the_user_asked_for(snapshot):
    result = retrieve(snapshot, "sections 5 and 3 of the gambling ordinance")
    reranker = CrossEncoderReranker(settings(qa_rerank_mode="on"), scorer=lambda q, p: [0.0] * len(p))
    ordered, method = rerank(reranker, result, snapshot)
    assert method == "exact" and [c.chunk.label for c in ordered] == ["5", "3"]


def test_mode_off_keeps_the_fused_order(snapshot):
    result = retrieve(snapshot, "punishment for gaming in a public place")
    ordered, method = rerank(CrossEncoderReranker(settings(qa_rerank_mode="off")), result, snapshot)
    assert method == "fused" and ordered == result.candidates


def test_cross_encoder_scores_reorder_the_head_and_keep_the_tail(snapshot):
    result = retrieve(snapshot, "punishment for gaming in a public place")
    seen = {}

    def scorer(query, passages):
        seen["passages"] = passages
        return [float(index) for index in range(len(passages))]      # reverse the order
    reranker = CrossEncoderReranker(settings(qa_rerank_mode="on", qa_rerank_candidates=3), scorer=scorer)
    ordered, method = rerank(reranker, result, snapshot)
    assert method == "cross_encoder"
    assert [c.chunk.id for c in ordered[:3]] == [c.chunk.id for c in reversed(result.candidates[:3])]
    assert ordered[3:] == result.candidates[3:] and ordered[0].reranker_score == 2.0
    assert seen["passages"][0].startswith(GAMBLING + " — Section 5 — Penalty")


def test_auto_mode_skips_a_reranker_that_cannot_meet_the_budget(snapshot):
    result = retrieve(snapshot, "punishment for gaming in a public place")
    slow = CrossEncoderReranker(settings(qa_rerank_mode="auto"), tokens_per_second=200)
    assert slow._affordable(8) == 1 and rerank(slow, result, snapshot)[1] == "fused"
    fast = CrossEncoderReranker(settings(qa_rerank_mode="auto"), tokens_per_second=50_000)
    assert fast._affordable(8) == 8
    uncalibrated = CrossEncoderReranker(settings(qa_rerank_mode="auto"))
    assert uncalibrated._affordable(8) == 0


def test_a_reranker_measured_too_slow_is_released_from_memory(snapshot, monkeypatch):
    def loaded(self):
        self._model, self._tokenizer = object(), object()

    def measured(speed):
        def calibrate(self):
            self.tokens_per_second = speed
        return calibrate

    monkeypatch.setattr(CrossEncoderReranker, "_load", loaded)
    monkeypatch.setattr(CrossEncoderReranker, "_calibrate", measured(200))
    slow = CrossEncoderReranker(settings(qa_rerank_mode="auto"), runtime=InferenceRuntime())
    asyncio.run(slow.warm())
    assert slow._model is None and slow._tokenizer is None and not slow.enabled
    result = retrieve(snapshot, "punishment for gaming in a public place")
    assert rerank(slow, result, snapshot)[1] == "fused"
    # "on" keeps a slow model because the operator asked for it; a fast host keeps it in "auto".
    forced = CrossEncoderReranker(settings(qa_rerank_mode="on"), runtime=InferenceRuntime())
    asyncio.run(forced.warm())
    assert forced._model is not None and forced.enabled
    monkeypatch.setattr(CrossEncoderReranker, "_calibrate", measured(50_000))
    fast = CrossEncoderReranker(settings(qa_rerank_mode="auto"), runtime=InferenceRuntime())
    asyncio.run(fast.warm())
    assert fast._model is not None and fast.enabled


def test_a_failing_or_missing_reranker_never_fails_the_question(snapshot):
    result = retrieve(snapshot, "punishment for gaming in a public place")

    def broken(query, passages):
        raise RuntimeError("out of memory")
    ordered, method = rerank(CrossEncoderReranker(settings(qa_rerank_mode="on"), scorer=broken), result, snapshot)
    assert method == "fused" and ordered == result.candidates
    wrong_count = CrossEncoderReranker(settings(qa_rerank_mode="on"), scorer=lambda q, p: [1.0])
    assert rerank(wrong_count, result, snapshot)[1] == "fused"
    # A model that is not installed locally disables the stage instead of raising.
    missing = CrossEncoderReranker(settings(qa_rerank_mode="on", qa_rerank_model_name="missing/not-installed"),
                                   runtime=InferenceRuntime())
    asyncio.run(missing.warm())
    assert rerank(missing, result, snapshot)[1] == "fused" and not missing.enabled


# -------------------------------------------------------------------- context
def test_requested_provisions_are_included_whole_and_in_order(snapshot):
    result = retrieve(snapshot, "sections 3 to 5 of the gambling ordinance")
    units = build_context(snapshot, result.candidates, result.analysis, settings())
    assert [(unit.key, unit.chunk.label, unit.relation) for unit in units[:3]] == [
        ("E1", "3", "requested"), ("E2", "4", "requested"), ("E3", "5", "requested")]
    assert units[0].evidence_id == f"chunk:{units[0].chunk.id}"


def test_conceptual_context_is_limited_to_the_best_provisions(snapshot):
    result = retrieve(snapshot, "punishment for gaming in a public place")
    units = build_context(snapshot, result.candidates, result.analysis, settings(qa_rerank_top_k=2))
    retrieved = [unit for unit in units if unit.relation == "retrieved"]
    assert len(retrieved) == 2 and retrieved[0].chunk.label == "5"


def test_the_best_provision_survives_any_budget(snapshot):
    result = retrieve(snapshot, "sections 3 to 5 of the gambling ordinance")
    units = build_context(snapshot, result.candidates, result.analysis,
                          settings(qa_context_max_chars=10, qa_context_max_units=1))
    assert [unit.chunk.label for unit in units] == ["3"]


def test_definitions_join_when_the_question_uses_a_defined_term(snapshot):
    result = retrieve(snapshot, "what is the penalty for keeping a common gaming house")
    units = build_context(snapshot, result.candidates, result.analysis, settings(qa_rerank_top_k=1))
    assert ("2", "definition") in [(unit.chunk.label, unit.relation) for unit in units]
    # A single ordinary word such as "consumer" pulls definitions in only for a definition question.
    scoped = retrieve(snapshot, "who is a consumer under the consumer protection act")
    assert scoped.analysis.wants_definition
    labels = [(snapshot.instruments[u.chunk.instrument_id].title, u.chunk.label)
              for u in build_context(snapshot, scoped.candidates, scoped.analysis, settings(qa_rerank_top_k=1))]
    assert (CONSUMER, "2") in labels
    none = build_context(snapshot, result.candidates, result.analysis,
                         settings(qa_rerank_top_k=1, qa_context_max_definition_sections=0))
    assert all(unit.relation != "definition" for unit in none)


def test_same_law_references_are_followed_but_other_laws_are_not(snapshot):
    result = retrieve(snapshot, "section 5-A of the gambling ordinance")      # refers to "section 5"
    units = build_context(snapshot, result.candidates, result.analysis, settings())
    assert [(unit.chunk.label, unit.relation) for unit in units] == [("5-A", "requested"), ("5", "reference")]
    claims = retrieve(snapshot, "section 25 of the consumer protection act")   # "as provided in section 4"
    followed = build_context(snapshot, claims.candidates, claims.analysis, settings())
    assert ("4", "reference") in [(unit.chunk.label, unit.relation) for unit in followed]
    off = build_context(snapshot, result.candidates, result.analysis,
                        settings(qa_context_max_reference_sections=0))
    assert [unit.chunk.label for unit in off] == ["5-A"]


def test_provisions_that_cite_the_best_passage_by_number_are_added(snapshot):
    # The penalty clause never mentions receipts; it only says "sections 18 to 22".
    result = retrieve(snapshot, "must a trader give the purchaser a receipt")
    units = build_context(snapshot, result.candidates, result.analysis, settings(qa_rerank_top_k=1))
    assert [(unit.chunk.label, unit.relation) for unit in units] == [("19", "retrieved"), ("32", "referring")]
    off = build_context(snapshot, result.candidates, result.analysis,
                        settings(qa_rerank_top_k=1, qa_context_max_referring_sections=0))
    assert [unit.chunk.label for unit in off] == ["19"]


def test_a_request_for_the_bare_text_of_a_provision_adds_no_citing_provisions(snapshot):
    bare = retrieve(snapshot, "What is section 19 of the Punjab Consumer Protection Act 2005?")
    units = build_context(snapshot, bare.candidates, bare.analysis, settings())
    assert [(unit.chunk.label, unit.relation) for unit in units] == [("19", "requested")]
    asked = retrieve(snapshot, "what is the punishment for breaking section 19 of the consumer protection act")
    units = build_context(snapshot, asked.candidates, asked.analysis, settings())
    assert [(unit.chunk.label, unit.relation) for unit in units] == [("19", "requested"), ("32", "referring")]


def test_citing_provisions_are_ranked_by_the_question_when_there_are_too_many(snapshot):
    result = retrieve(snapshot, "section 4 of the consumer protection act and the punishment for a manufacturer")
    units = build_context(snapshot, result.candidates, result.analysis,
                          settings(qa_context_max_referring_sections=1))
    # Section 4 is cited by sections 25 (claims) and 32 (penalties); the question is about punishment.
    assert [(unit.chunk.label, unit.relation) for unit in units] == [("4", "requested"), ("32", "referring")]
