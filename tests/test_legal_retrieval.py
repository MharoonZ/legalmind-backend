import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.repositories.legal_search_repository import PublicationChanged
from app.schemas.legal_search import LegalSearchIn
from app.services.legal_query import parse_references, prepare_query, query_alternative, reference_parts, resolve_laws
from app.services.legal_retrieval import LegalRetrievalService, reciprocal_rank_fusion
from app.services.retrieval_fusion import filter_keyword_candidates

CONSUMER, GAMBLING, PUBLICATION = uuid4(), uuid4(), uuid4()
CATALOG = [
    {"instrument_id": CONSUMER, "canonical_title": "The Punjab Consumer Protection Act 2005",
     "citation_key": "consumer-protection-act-2005", "aliases": ["Consumer Protection Act"],
     "jurisdiction_code": "PK-PB", "edition_key": "source-2026"},
    {"instrument_id": GAMBLING, "canonical_title": "The Punjab Prevention of Gambling Ordinance, 1978",
     "citation_key": "gambling-1978", "aliases": [], "jurisdiction_code": "PK-PB", "edition_key": "source-2026"},
]


def candidate(identity=None, **kwargs):
    return {"chunk_id": identity or uuid4(), "chunk_set_id": uuid4(), "revision_id": uuid4(),
        "root_provision_id": uuid4(), "instrument_id": CONSUMER, "legal_version_id": uuid4(),
        "instrument_title": CATALOG[0]["canonical_title"], "citation_key": "consumer-protection",
        "edition_key": "source-2026", "jurisdiction_code": "PK-PB", "applicability_status": "unknown",
        "kind": "section", "label": "19", "heading": "Receipt", "reference_path": "f:source/s:19",
        "matched_reference_paths": ["f:source/s:19"], "sequence_in_root": 1, "root_total_chunks": 1,
        "chunk_text": "Receipt shall be issued", "source_refs": [{"page_index": 4}], **kwargs}


class Repository:
    def __init__(self, rows=([], [], []), catalog=CATALOG):
        self.rows, self.catalog, self.calls = rows, catalog, []
    async def active_catalog(self):
        return ({"id": PUBLICATION, "name": "test"}, self.catalog) if self.catalog is not None else (None, [])
    async def retrieve(self, publication_id, **kwargs):
        assert publication_id == PUBLICATION
        self.calls.append(kwargs)
        return self.rows


async def encode(texts, profile, **kwargs):
    return [[1.0] + [0.0] * 1023]


def run(request, repository, settings, encoder=encode):
    return asyncio.run(LegalRetrievalService(repository, settings, encode=encoder).search(LegalSearchIn(**request)))


def test_rrf_uses_rank_not_incommensurable_raw_scores():
    a, b = uuid4(), uuid4()
    rows = reciprocal_rank_fusion([candidate(a, keyword_score=1000), candidate(b, keyword_score=.01)],
                                 [candidate(b, vector_score=.8)])
    assert rows[0]["chunk_id"] == b
    assert rows[0]["rrf_score"] == pytest.approx(1/62 + 1/61)
    assert rows[0]["keyword_rank"] == 2 and rows[0]["vector_rank"] == 1


def test_rrf_deduplicates_each_branch():
    a = candidate(keyword_score=2)
    result = reciprocal_rank_fusion([a, a], [])
    assert len(result) == 1 and result[0]["rrf_score"] == pytest.approx(1/61)


@pytest.mark.parametrize("strict,similarity,available,matched,total,accepted", [
    (True, .1, True, 1, 1, True),
    (False, .2, True, 2, 6, False),
    (False, .7, True, 2, 6, True),
    (False, None, False, 2, 6, False),
    (False, None, False, 3, 4, True),
])
def test_relaxed_keyword_overlap_needs_semantics_or_strong_fallback_coverage(strict, similarity, available, matched, total, accepted):
    row = {"keyword_strict_match": strict, "lexical_vector_score": similarity,
           "keyword_matched_terms": matched, "keyword_total_terms": total}
    assert bool(filter_keyword_candidates([row], semantic_available=available, min_similarity=.48)) is accepted


@pytest.mark.parametrize("query,label,children", [
    ("Section 19", "19", []), ("sec. 19A", "19A", []),
    ("Sectoin １９", "19", []), ("subsection (2)(a) of section 4", "4", ["2", "a"]),
    ("section 19(2)(a)", "19", ["2", "a"]), ("Rule 7", "7", []),
    ("Article 25", "25", []), ("Schedule IV", "IV", [])])
def test_exact_reference_parser_preserves_identifier_boundaries(query, label, children):
    refs = parse_references(query)
    assert len(refs) == 1 and reference_parts(refs[0]) == (label, children)


def test_differently_worded_questions_take_semantic_path(settings):
    repo = Repository(([], [], [candidate(vector_score=.7)]))
    response = run({"query": "The shopkeeper refused to give me proof of purchase"}, repo, settings)
    assert response.status == "ok" and response.results[0].vector_rank == 1
    assert repo.calls[0]["query_vector"] is not None


def test_conceptual_question_can_search_two_explicitly_named_published_laws(settings):
    repo = Repository(([], [], [candidate(vector_score=.7)]))
    response = run({"query": "Compare duties under Consumer Protection Act and under Prevention of Gambling Ordinance."},
                   repo, settings)
    assert response.status == "ok"
    assert set(repo.calls[0]["instrument_ids"]) == {CONSUMER, GAMBLING}
    assert repo.calls[0]["instrument_id"] is None


def test_no_publication_does_not_load_encoder(settings):
    async def forbidden(*a, **k): pytest.fail("No corpus: must not load model")
    response = run({"query": "consumer receipts"}, Repository(catalog=None), settings, forbidden)
    assert response.status == "no_active_publication" and response.results == []


def test_empty_retrieval_does_not_manufacture_answer(settings):
    response = run({"query": "astronaut pizza on Mars"}, Repository(), settings)
    assert response.status == "no_results" and not response.results


def test_exact_lookup_does_not_encode_or_fall_back_to_other_sections(settings):
    async def forbidden(*a, **k): pytest.fail("Exact lookup should not load the model")
    response = run({"query": "Section 19", "law": "Consumer Protection Act"},
                   Repository(([candidate()], [], [])), settings, forbidden)
    assert response.status == "ok" and response.results[0].exact_rank == 1
    assert response.results[0].keyword_rank is None and response.results[0].rrf_rank is None


def test_ambiguous_reference_requests_law_clarification(settings):
    rows = [candidate(), candidate(instrument_id=GAMBLING, matched_reference_paths=["f:another/s:19"])]
    response = run({"query": "Section 19"}, Repository((rows, [], [])), settings)
    assert response.status == "needs_clarification" and response.results == []
    assert len(response.clarification_options) == 2


def test_plural_or_compound_references_are_not_silently_narrowed(settings):
    response = run({"query": "Sections 19 and 20"}, Repository(), settings)
    assert response.status == "needs_clarification"


def test_every_reference_in_a_numbered_list_is_resolved_in_its_selected_law(settings):
    class Multi(Repository):
        async def retrieve(self, publication_id, **kwargs):
            self.calls.append(kwargs)
            label = kwargs["reference"].label
            if label == "999":
                return [], [], []
            row = {**candidate(), "label": label, "matched_reference_paths": [f"f:source/s:{label}"]}
            return [row], [], []

    repo = Multi()
    response = run({"query": "Sections 19, 20, and 999 of the Consumer Protection Act"}, repo, settings)
    assert response.status == "ok" and response.missing_references == ["section 999"]
    assert [row.label for row in response.results] == ["19", "20"]
    assert [row.exact_rank for row in response.results] == [1, 2]
    assert all(call["instrument_id"] == CONSUMER and call["query_vector"] is None for call in repo.calls)


@pytest.mark.parametrize("query", [
    "Sections 19, 20 to 22 of the Consumer Protection Act",
    "Sections 19(1) and (2) of the Consumer Protection Act",
    "Section 99999999999999999999999 of the Consumer Protection Act",
    "Section 19 of the Contract Act 1872 and Section 19 of the Consumer Protection Act",
])
def test_unresolved_combined_reference_never_falls_back_to_the_first_number(query, settings):
    repo = Repository()
    assert run({"query": query}, repo, settings).status == "needs_clarification"
    assert not repo.calls


def test_conflicting_explicit_edition_cannot_resolve_to_the_current_act(settings):
    repo = Repository(([candidate()], [], []))
    out = run({"query": "Section 19 of the Consumer Protection Act 1999"}, repo, settings)
    assert out.status == "no_results" and not repo.calls


def test_exact_lookup_can_correct_a_misspelled_title_without_correcting_its_year(settings):
    repo = Repository(([candidate()], [], []))
    query = "Section 19 of the Consuemr Protecion Act"
    assert run({"query": query}, repo, settings).status == "ok"
    assert repo.calls[0]["instrument_id"] == CONSUMER
    assert run({"query": query + " 1999"}, repo, settings).status == "no_results"


def test_original_wording_and_repaired_keywords_are_both_used(settings):
    repo = Repository(([], [candidate(keyword_score=1)], []))
    original = "Must the shopkepper give a reciept to the purchser?"
    assert query_alternative(original) == "Must the shopkeeper give a receipt to the purchaser?"
    response = run({"query": original}, repo, settings)
    assert response.query == original and response.retrieval_query == original
    assert "reciept" in repo.calls[0]["query"]
    assert "receipt" in repo.calls[0]["alternative_keyword"]
    assert query_alternative("Has the law changed?") is None


def test_empty_first_pass_retries_with_a_bounded_semantic_alternative(settings):
    class Retry(Repository):
        async def retrieve(self, publication_id, **kwargs):
            self.calls.append(kwargs)
            return ([], [], [candidate(vector_score=.7)]) if len(self.calls) == 2 else ([], [], [])

    encoded = []

    async def record_encode(texts, profile, **kwargs):
        encoded.extend(texts)
        return await encode(texts, profile, **kwargs)

    repo = Retry()
    result = run({"query": "Must I get proof of purchase?"}, repo, settings, record_encode)
    assert result.status == "ok" and result.warnings
    assert encoded == ["Must I get proof of purchase?", "Must I get receipt?"]
    assert repo.calls[0]["pool_size"] < repo.calls[1]["pool_size"] <= 100
    assert repo.calls[0]["min_similarity"] == repo.calls[1]["min_similarity"]


def test_empty_lexical_results_with_a_failed_encoder_are_a_service_failure(settings):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("private model detail")

    result = run({"query": "receipt"}, Repository(), settings, unavailable)
    assert result.status == "service_unavailable" and not result.results
    assert "private" not in result.message


def test_database_failure_is_a_service_failure_with_no_parameters(settings):
    from sqlalchemy.exc import OperationalError

    class Offline(Repository):
        async def active_catalog(self):
            raise OperationalError("SELECT ...", {"secret": "private"}, RuntimeError())

    result = run({"query": "receipt"}, Offline(), settings)
    assert result.status == "service_unavailable"
    assert "private" not in result.message


def test_explicit_followup_uses_the_new_reference_and_preserves_previous_context():
    request = LegalSearchIn(query="What does Section 32 say?",
        context={"previous_query": "Tell me about Section 31 of the Prisons Act, 1894."})
    request._explicit_followup = True
    prepared = prepare_query(request)
    assert [reference.label for reference in prepared.references] == ["32"]
    assert "Section 31" in prepared.text and prepared.context_used


def test_explicit_reference_to_absent_law_does_not_search_other_acts(settings):
    repo = Repository()
    response = run({"query": "Section 19 of the Contract Act 1872"}, repo, settings)
    assert response.status == "no_results" and not repo.calls


def test_reference_law_title_can_omit_year_and_jurisdiction(settings):
    repo = Repository(([candidate()], [], []))
    response = run({"query": "Section 19 of the Consumer Protection Act"}, repo, settings)
    assert response.status == "ok" and repo.calls[0]["instrument_id"] == CONSUMER


def test_explicit_absent_code_does_not_resolve_to_a_different_published_law(settings):
    repo = Repository()
    response = run({"query": "Section 19 of the Pakistan Penal Code 1860"}, repo, settings)
    assert response.status == "no_results" and not repo.calls


def test_reference_to_this_act_uses_explicit_previous_context(settings):
    repo = Repository(([candidate()], [], []))
    response = run({"query": "What does Section 19 of this Act require?", "context": {
        "previous_query": "The Punjab Consumer Protection Act 2005"}}, repo, settings)
    assert response.status == "ok" and response.context_used
    assert repo.calls[0]["instrument_id"] == CONSUMER


def test_unpublished_instrument_is_not_searched(settings):
    repo = Repository()
    response = run({"query": "receipt", "instrument_id": str(uuid4())}, repo, settings)
    assert response.status == "no_results" and not repo.calls


def test_conflicting_explicit_law_and_instrument_are_not_silently_overridden(settings):
    response = run({"query": "Section 19", "law": "Consumer Protection Act", "instrument_id": str(GAMBLING)}, Repository(), settings)
    assert response.status == "needs_clarification"


def test_followup_uses_only_supplied_context(settings):
    repo = Repository()
    response = run({"query": "What if they refuse to issue it?", "context": {
        "previous_query": "Receipt duties under the Consumer Protection Act", "instrument_id": str(CONSUMER)}}, repo, settings)
    assert response.context_used
    assert "Receipt duties" in response.retrieval_query
    assert repo.calls[0]["instrument_id"] == CONSUMER


def test_context_does_not_override_a_new_explicit_law(settings):
    repo = Repository()
    run({"query": "What about Section 4 of the Punjab Prevention of Gambling Ordinance, 1978?",
        "context": {"previous_query": "Consumer receipts", "instrument_id": str(CONSUMER)}}, repo, settings)
    assert repo.calls[0]["instrument_id"] == GAMBLING


def test_contextual_law_does_not_override_a_new_named_law_in_a_general_question(settings):
    repo = Repository()
    run({"query": "What about penalties in The Punjab Prevention of Gambling Ordinance, 1978?",
        "context": {"previous_query": "Consumer receipts", "law": "Consumer Protection Act"}}, repo, settings)
    assert repo.calls[0]["instrument_id"] == GAMBLING


def test_missing_followup_context_asks_for_clarification(settings):
    response = run({"query": "What about it?"}, Repository(), settings)
    assert response.status == "needs_clarification"


def test_encoder_failure_is_an_explicit_lexical_fallback(settings):
    async def unavailable(*a, **k): raise RuntimeError("model unavailable")
    response = run({"query": "receipt purchaser"}, Repository(([], [candidate(keyword_score=1)], [])), settings, unavailable)
    assert response.status == "ok" and response.warnings and response.results[0].vector_rank is None


def test_publication_change_is_retried_without_mixing_candidates(settings):
    class Changed(Repository):
        async def retrieve(self, *a, **k):
            self.calls.append(k)
            raise PublicationChanged()
    repo = Changed()
    response = run({"query": "receipt"}, repo, settings)
    assert response.status == "no_active_publication" and len(repo.calls) == 2 and not response.results


@pytest.mark.parametrize("payload", [{"query": " "}, {"query": "!!!"}, {"query": "x", "limit": 0},
    {"query": "x", "limit": 51}, {"query": "x", "revision_ids": [str(uuid4())]},
    {"query": "section 19", "reference": {"label": "19()"}}])
def test_request_rejects_invalid_or_unscoped_inputs(payload):
    with pytest.raises(ValidationError): LegalSearchIn(**payload)


def test_search_bounds_combined_followup_before_retrieval():
    with pytest.raises(ValidationError):
        LegalSearchIn(query="x" * 2000, context={"previous_query": "x" * 2000})


def test_selected_instrument_resolves_a_shared_law_title(settings):
    other = {**CATALOG[0], "instrument_id": GAMBLING, "canonical_title": "Consumer Protection Act 2005"}
    repo = Repository(([candidate()], [], []), catalog=[CATALOG[0], other])
    response = run({"query": "Section 19", "law": "Consumer Protection Act",
                    "instrument_id": str(CONSUMER)}, repo, settings)
    assert response.status == "ok" and repo.calls[0]["instrument_id"] == CONSUMER
