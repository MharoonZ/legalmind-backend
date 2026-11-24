"""Query understanding and first-stage retrieval over the in-memory publication."""
import asyncio

import numpy as np
import pytest

from app.core.inference import InferenceRuntime
from app.retrieval.index import PublicationIndex
from app.retrieval.query import analyze, has_topic, wants_definition
from app.retrieval.service import QueryEncoder, RetrievalService
from app.schemas.legal_search import LegalSearchIn
from tests.corpus import CONSUMER, GAMBLING, PRISONERS, PRISONS, REPEAL, fake_encode, law_id, make_snapshot, settings


@pytest.fixture(scope="module")
def snapshot():
    return make_snapshot()


def service(snapshot, *, encode=fake_encode, **overrides):
    encoder = QueryEncoder(runtime=InferenceRuntime(), encode=encode)
    return RetrievalService(PublicationIndex.from_snapshot(snapshot), settings(**overrides), encoder=encoder)


def retrieve(snapshot, question, **kwargs):
    return asyncio.run(service(snapshot).retrieve(question, **kwargs))


def labels(result):
    return [(result.snapshot.instruments[c.chunk.instrument_id].title, c.chunk.label) for c in result.candidates]


# ------------------------------------------------------------------ analysis
def test_the_reported_misspelt_law_resolves_like_the_correct_spelling(snapshot):
    correct = analyze(snapshot, "What is Section 3 of Punjab Prevention of Gambling Ordinance 1978?")
    misspelt = analyze(snapshot, "What is Section 3 of Punjb Prevention of Gambling Ordinance 1978?")
    for analysis in (correct, misspelt):
        assert analysis.scope == (law_id(GAMBLING),) and analysis.hard_scope
        assert [ref.display for ref in analysis.references] == ["Section 3"]


def test_trailing_name_without_a_type_word_names_the_law(snapshot):
    assert analyze(snapshot, "what is section 3 of gambling").scope[0] == law_id(GAMBLING)
    unknown = analyze(snapshot, "what is article 5 of the constitution")
    assert unknown.law.status == "unresolved" and unknown.law.mention == "constitution"
    pronoun = analyze(snapshot, "what is section 3 of this act")
    assert pronoun.law.status == "none"


def test_follow_up_inherits_the_law_from_the_conversation(snapshot):
    history = [("user", "What is Section 3 of the Punjab Prevention of Gambling Ordinance 1978?"),
               ("assistant", "Section 3 sets the penalty for keeping a common gaming house [1].")]
    analysis = analyze(snapshot, "and section 4?", history=history)
    assert analysis.scope == (law_id(GAMBLING),) and analysis.law_source == "context"
    assert analysis.followup and analysis.context_used and not analysis.hard_scope


def test_law_named_only_by_the_assistant_is_carried_over(snapshot):
    history = [("user", "What is the punishment for gambling in a public place?"),
               ("assistant", "Under Section 5 of the Punjab Prevention of Gambling Ordinance, 1978 it is punishable [1].")]
    assert analyze(snapshot, "what does section 4 say?", history=history).scope == (law_id(GAMBLING),)


def test_a_reply_naming_only_a_law_completes_the_earlier_question(snapshot):
    history = [("user", "What is Section 3?"), ("assistant", "Which law is Section 3 from?")]
    analysis = analyze(snapshot, "the gambling ordinance", history=history)
    assert analysis.scope == (law_id(GAMBLING),) and analysis.law_source == "question"
    assert [ref.display for ref in analysis.references] == ["Section 3"]


@pytest.mark.parametrize("reply,expected", [("the second one", PRISONERS), ("2", PRISONERS), ("first", PRISONS),
                                            ("the latter", PRISONERS), ("option 1", PRISONS)])
def test_an_ordinal_reply_picks_from_the_laws_the_assistant_listed(snapshot, reply, expected):
    history = [("user", "what is section 3 of the prison act"),
               ("assistant", f"More than one law matches that name. Which one do you mean?\n1. {PRISONS}\n2. {PRISONERS}")]
    analysis = analyze(snapshot, reply, history=history)
    assert analysis.scope == (law_id(expected),)
    assert [ref.display for ref in analysis.references] == ["Section 3"]


def test_a_new_topic_does_not_inherit_the_previous_law(snapshot):
    history = [("user", "What is Section 3 of the Punjab Prevention of Gambling Ordinance 1978?"),
               ("assistant", "Section 3 sets the penalty for keeping a common gaming house.")]
    analysis = analyze(snapshot, "Must a trader issue a receipt to the purchaser of goods?", history=history)
    assert not analysis.followup and not analysis.scope and analysis.search_text == analysis.question


def test_an_unknown_named_law_is_never_replaced_by_a_remembered_one(snapshot):
    history = [("user", "What is Section 3 of the Punjab Prevention of Gambling Ordinance 1978?")]
    analysis = analyze(snapshot, "and what about section 4 of the Pakistan Penal Code?", history=history)
    assert analysis.law.status == "unresolved" and analysis.law.mention == "Pakistan Penal Code"


def test_explicit_request_fields_take_priority(snapshot):
    by_id = analyze(snapshot, "what does section 2 say", instrument_id=law_id(CONSUMER))
    assert by_id.scope == (law_id(CONSUMER),) and by_id.law_source == "request"
    by_hint = analyze(snapshot, "what does section 2 say", law_hint="consumer protecton act")
    assert by_hint.scope == (law_id(CONSUMER),)
    by_context = analyze(snapshot, "and section 4?", context_law="Punjab Consumer Protection Act 2005")
    assert by_context.scope == (law_id(CONSUMER),) and by_context.law_source == "context"
    by_context_id = analyze(snapshot, "and section 4?", context_instrument_id=law_id(PRISONS))
    assert by_context_id.scope == (law_id(PRISONS),)
    elsewhere = analyze(snapshot, "section 3 of the prisons act", jurisdiction="PK-SD")
    assert elsewhere.law.status == "unresolved"


def test_pronoun_follow_up_searches_with_the_previous_question(snapshot):
    analysis = analyze(snapshot, "What if they refuse to issue it?",
                       previous_query="Does a seller have to give a purchaser a receipt?")
    assert analysis.followup and analysis.context_used
    assert analysis.search_text.startswith("Does a seller have to give")


def test_definition_questions_and_topics_are_recognised(snapshot):
    assert wants_definition("What is a common gaming house?")
    assert wants_definition("who is a consumer under the consumer protection act")
    assert not wants_definition("what is the punishment for gambling in a public place?")
    assert not wants_definition("what is section 3 of the gambling ordinance")
    assert not has_topic(analyze(snapshot, "What is Section 3?"))
    assert has_topic(analyze(snapshot, "section 3 wall chalking"))


# ----------------------------------------------------------------- retrieval
def test_exact_lookup_for_the_reported_question(snapshot):
    result = retrieve(snapshot, "What is Section 3 of Punjb Prevention of Gambling Ordinance 1978?")
    assert result.status == "ok" and labels(result) == [(GAMBLING, "3")]
    assert result.candidates[0].exact_rank == 1
    response = result.to_response()
    assert response.status == "ok" and response.results[0].label == "3"
    assert response.results[0].source_refs[0]["page_index"] == 4
    assert response.publication_id == snapshot.id


def test_lists_ranges_and_nested_references(snapshot):
    assert labels(retrieve(snapshot, "sections 3 and 5 of the gambling ordinance")) == [(GAMBLING, "3"), (GAMBLING, "5")]
    ranged = retrieve(snapshot, "What are sections 3 to 5 of the gambling ordinance?")
    assert [label for _, label in labels(ranged)] == ["3", "4", "5"]
    nested = retrieve(snapshot, "section 3(1)(a) of the gambling ordinance")
    assert nested.candidates[0].matched_reference_paths == ("f:test/s:3/sub:1/p:a",)
    absent_child = retrieve(snapshot, "section 3(9) of the gambling ordinance")
    assert absent_child.status == "ok" and absent_child.warnings


def test_missing_provisions_are_reported_not_substituted(snapshot):
    missing = retrieve(snapshot, "What is section 99 of the gambling ordinance?")
    assert missing.status == "no_results" and missing.missing_references == ["Section 99"]
    assert "could not be found" in missing.message and GAMBLING in missing.message
    partial = retrieve(snapshot, "sections 3 and 99 of the gambling ordinance")
    assert partial.status == "ok" and partial.missing_references == ["Section 99"]
    assert labels(partial) == [(GAMBLING, "3")]


def test_a_law_outside_the_collection_is_named_as_unknown(snapshot):
    exact = retrieve(snapshot, "What is section 302 of the Pakistan Penal Code?")
    assert exact.status == "no_results" and exact.unknown_law == "Pakistan Penal Code"
    assert not exact.candidates
    topic = retrieve(snapshot, "What is the punishment for gaming under the Pakistan Penal Code?")
    assert topic.status == "ok" and topic.unknown_law == "Pakistan Penal Code" and topic.candidates


def test_ambiguous_law_names_ask_or_narrow_by_the_provision(snapshot):
    ambiguous = retrieve(snapshot, "what is section 3 of the prison act")
    assert ambiguous.status == "needs_clarification"
    assert {option["instrument_title"] for option in ambiguous.clarification_options} == {PRISONS, PRISONERS}
    # Only the Prisons Act has a section 9, so the name is no longer ambiguous.
    narrowed = retrieve(snapshot, "what is section 9 of the prison act")
    assert narrowed.status == "ok" and labels(narrowed) == [(PRISONS, "9")]


def test_bare_reference_infers_the_law_from_the_topic_or_asks(snapshot):
    asked = retrieve(snapshot, "What is Section 3?")
    assert asked.status == "needs_clarification" and len(asked.clarification_options) >= 2
    inferred = retrieve(snapshot, "section 19 receipt to the purchaser")
    assert inferred.status == "ok" and inferred.inferred_law == law_id(CONSUMER)
    assert labels(inferred) == [(CONSUMER, "19")]
    unique = retrieve(snapshot, "what is section 31?")        # only one law has a section 31
    assert labels(unique) == [(PRISONS, "31")]


def test_hybrid_search_handles_typos_and_paraphrases(snapshot):
    clean = retrieve(snapshot, "What is the punishment for gambling in a public place?")
    typos = retrieve(snapshot, "wat is the punishmnt for gamblng in publc place")
    assert labels(clean)[0] == labels(typos)[0] == (GAMBLING, "5")
    assert typos.spelling_repairs == {"gamblng": "gambling", "publc": "public"}
    assert clean.candidates[0].keyword_rank == 1 and clean.candidates[0].vector_rank == 1
    receipt = retrieve(snapshot, "must a trader issue a receipt to the purchaser")
    assert labels(receipt)[0] == (CONSUMER, "19")


def test_a_named_law_restricts_search_and_falls_back_when_it_has_nothing(snapshot):
    scoped = retrieve(snapshot, "what does the consumer protection act say about defective products")
    assert {law for law, _ in labels(scoped)} == {CONSUMER}
    fallback = retrieve(snapshot, "what does the repeal act say about a receipt for the purchaser",
                        law_hint=REPEAL)
    assert fallback.status == "ok" and fallback.candidates


def test_remembered_law_only_biases_a_conceptual_follow_up(snapshot):
    history = [("user", "What is Section 5 of the Punjab Prevention of Gambling Ordinance 1978?")]
    result = retrieve(snapshot, "and what about the penalty in a gaming house?", history=history)
    assert result.analysis.law_source == "context" and labels(result)[0][0] == GAMBLING


def test_only_a_law_named_returns_its_opening_provisions(snapshot):
    result = retrieve(snapshot, "Tell me about the Punjab Consumer Protection Act")
    assert result.status == "ok" and result.overview
    assert [label for _, label in labels(result)] == ["2", "4", "19", "25"]
    misspelt = retrieve(snapshot, "tell me about the consumer protecton act")
    assert misspelt.overview
    topical = retrieve(snapshot, "what does the consumer protection act say about receipts")
    assert not topical.overview


def test_no_topic_and_no_context_asks_what_is_meant(snapshot):
    result = retrieve(snapshot, "What about it?")
    assert result.status == "needs_clarification" and "law or topic" in result.message


def test_keyword_search_still_works_when_the_encoder_fails(snapshot):
    def broken(text, profile):
        raise RuntimeError("model missing")
    result = asyncio.run(service(snapshot, encode=broken).retrieve("punishment for gaming in a public place"))
    assert result.status == "ok" and labels(result)[0] == (GAMBLING, "5")
    assert any("keyword search only" in warning for warning in result.warnings)
    assert all(candidate.vector_rank is None for candidate in result.candidates)


def test_a_question_matching_nothing_returns_no_results(snapshot):
    result = asyncio.run(service(snapshot, legal_search_min_similarity=0.9).retrieve(
        "Andromeda galaxy distance"))
    assert result.status == "no_results" and not result.candidates
    assert "No relevant passages" in result.message


def test_no_publication_and_internal_errors_are_service_states(snapshot):
    empty = RetrievalService(PublicationIndex.from_snapshot(None), settings())
    assert asyncio.run(empty.retrieve("anything")).status == "no_active_publication"

    class Exploding(QueryEncoder):
        async def encode(self, text, profile):
            raise ValueError("unexpected")
    failing = RetrievalService(PublicationIndex.from_snapshot(snapshot), settings(), encoder=Exploding())
    assert asyncio.run(failing.retrieve("punishment for gaming")).status == "service_unavailable"


def test_search_endpoint_contract_and_explicit_reference(snapshot):
    api = service(snapshot)
    response = asyncio.run(api.search(LegalSearchIn(
        query="what does it say", law="gambling ordinance", reference={"kind": "section", "label": "5-A"})))
    assert response.status == "ok" and response.results[0].label == "5-A" and response.results[0].exact_rank == 1
    hybrid = asyncio.run(api.search(LegalSearchIn(query="receipt for goods sold", limit=2)))
    assert len(hybrid.results) == 2 and hybrid.results[0].rrf_rank == 1 and hybrid.results[0].rrf_score > 0


def test_query_vectors_are_cached_and_validated(snapshot):
    calls = []

    def counting(text, profile):
        calls.append(text)
        return fake_encode(text, profile)
    encoder = QueryEncoder(runtime=InferenceRuntime(), encode=counting, cache_size=2)
    for text in ("a question", "a question", "another", "a third", "a question"):
        assert asyncio.run(encoder.encode(text, snapshot.profile)) is not None
    assert calls == ["a question", "another", "a third", "a question"]
    wrong_size = QueryEncoder(runtime=InferenceRuntime(), encode=lambda text, profile: np.ones(3))
    assert asyncio.run(wrong_size.encode("x", snapshot.profile)) is None
