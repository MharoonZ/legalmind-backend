"""Citation boundary and stored-offset checks, without a live DB or model."""
import asyncio
from types import SimpleNamespace as Row

import pytest

from app.repositories.qa_context_repository import ContextCatalog, ContextScopeError
from app.schemas.qa import AskIn
from app.services.qa_answer import QuestionAnswerService
from app.services.qa_citations import CitationVerificationError, CitationVerifier, _quote_span
from app.services.qa_source_verification import SourceProof
from tests.qa_fixtures import identity
from tests.test_qa_answer import FakeGenerator, FakePrep
from tests.test_qa_citations import FakeSource, setup
from tests.test_qa_gate_safety import fact_decision
from tests.test_qa_source_verification import FakeSession, prepared_source


def proof_for(unit, start=0, end=None, page=1):
    return SourceProof(start=start, end=len(unit.text) if end is None else end,
        provision_id=unit.provision_id, kind=unit.kind, label=unit.label, reference_path=unit.reference_path,
        source_span_id=identity(f"span-{page}"), file_id=identity("file"), file_sha256="a" * 64,
        filename="synthetic.pdf", page_index=page, source_url=None)


@pytest.mark.parametrize("fault", ["missing_prefix", "missing_tail", "middle_gap", "negative", "overshoot", "empty"])
def test_citation_layer_rechecks_complete_coverage_from_its_source_adapter(fault):
    decision, draft = setup()
    unit = decision.context.units[0]
    proofs = {
        "missing_prefix": [proof_for(unit, 5)],
        "missing_tail": [proof_for(unit, end=len(unit.text) - 5)],
        "middle_gap": [proof_for(unit, end=5), proof_for(unit, 15)],
        "negative": [proof_for(unit, -1)],
        "overshoot": [proof_for(unit, end=len(unit.text) + 10)],
        "empty": [proof_for(unit, end=0)],
    }[fault]
    with pytest.raises(CitationVerificationError):
        asyncio.run(CitationVerifier(FakeSource(proofs)).verify(draft, decision))


def test_whitespace_gap_is_allowed_but_citations_are_in_source_order():
    decision, draft = setup()
    unit = decision.context.units[0]
    split = unit.text.index(" ")
    proofs = [proof_for(unit, split + 1, page=2), proof_for(unit, end=split)]
    result = asyncio.run(CitationVerifier(FakeSource(proofs)).verify(draft, decision))
    assert [c.page_index for c in result.citations] == [1, 2]
    assert result.claims[0].citation_indexes == (0, 1)
    assert all(c.quote for c in result.citations)


@pytest.mark.parametrize("text,quote", [
    ("It is unlawful to enter the restricted premises.", "lawful to enter the restricted premises."),
    ("The permit remains subject to cancellation.", "The permit remains subject to cancel"),
    ("The procedure under section 149 applies.", "The procedure under section 14"),
    ("The label is prohibited_entry for the restricted premises.", "entry for the restricted premises."),
])
def test_quote_cannot_start_or_end_inside_a_source_word_or_number(text, quote):
    decision, draft = setup()
    unit = decision.context.units[0].model_copy(update={"text": text})
    context = decision.context.model_copy(update={"units": (unit, *decision.context.units[1:])})
    claim = draft.claims[0].model_copy(update={"quote": quote, "claim_text": quote})
    source = FakeSource()
    with pytest.raises(CitationVerificationError, match="quotation_mismatch"):
        asyncio.run(CitationVerifier(source).verify(draft.model_copy(update={"claims": [claim]}),
            decision.model_copy(update={"context": context})))
    assert source.calls == []


def test_repeated_text_skips_a_word_fragment_and_finds_the_later_whole_word_quote():
    text = "unlawful to enter the premises. It is lawful to enter the premises."
    quote = "lawful to enter the premises."
    start, end = _quote_span(text, quote)
    assert start == text.rindex(quote) and text[start:end] == quote


@pytest.mark.parametrize("quote", ["(1) A written permit is required.", "A written\npermit is\trequired."])
def test_punctuation_and_whitespace_normalization_still_allow_exact_wording(quote):
    text = "(1) A written permit is required."
    span = _quote_span(text, quote)
    assert span is not None and " ".join(text[slice(*span)].split()) == " ".join(quote.split())


@pytest.mark.parametrize("quote", ["A written permit is Required.", "A written permit is required!", "A written permit is not required."])
def test_case_punctuation_and_legal_word_changes_are_not_normalized_away(quote):
    assert _quote_span("A written permit is required.", quote) is None


@pytest.mark.parametrize("fault", ["block_overshoot", "block_end_missing", "chunk_overshoot", "provision_overshoot", "all_overshoot"])
def test_stored_chunk_source_ranges_cannot_be_approved_by_slice_clipping(fault):
    verifier, catalog, unit = prepared_source()
    link, span, _, _, _, _, _, _ = catalog.session.rows[0]
    if fault in {"block_overshoot", "all_overshoot"}:
        span.block_char_end += 10
    elif fault == "block_end_missing":
        span.block_char_end = None
    if fault in {"chunk_overshoot", "all_overshoot"}:
        link.chunk_char_end += 10
    if fault in {"provision_overshoot", "all_overshoot"}:
        link.provision_char_end += 10
    if fault == "all_overshoot":
        span.provision_char_end += 10
    with pytest.raises(CitationVerificationError, match="source_mapping_not_exact"):
        asyncio.run(verifier.verify(unit, 0, len(unit.text)))


@pytest.mark.parametrize("field,value", [("chunk_char_start", -1), ("chunk_char_end", 0),
    ("chunk_char_start", True), ("provision_char_end", None)])
def test_malformed_chunk_offsets_fail_with_a_controlled_citation_error(field, value):
    verifier, catalog, unit = prepared_source()
    setattr(catalog.session.rows[0][0], field, value)
    with pytest.raises(CitationVerificationError):
        asyncio.run(verifier.verify(unit, 0, len(unit.text)))


def provision_catalog():
    _, original, unit = prepared_source()
    _, span, node, page, block, _, file, _ = original.session.rows[0]
    node.text_sha256 = block.text_sha256 = "b" * 64
    block.id, page.id = identity("block"), identity("page")
    file.publisher = None
    extraction, source = Row(id=identity("extraction")), Row(source_role="primary")
    session = FakeSession([(span, node, page, block, extraction, file, source)])
    catalog = ContextCatalog(session, original.items, {}, {node.id: node}, {}, {})
    return catalog, unit, span


def test_valid_provision_mapping_retains_its_canonical_source_metadata():
    catalog, unit, _ = provision_catalog()
    refs = asyncio.run(catalog.provision_sources({unit.provision_id}))[unit.provision_id]
    assert refs[0]["provision_char_start"] == 0 and refs[0]["provision_char_end"] == len(unit.text)
    assert refs[0]["file_sha256"] == "a" * 64 and refs[0]["page_index"] == 1


@pytest.mark.parametrize("fault", ["block_overshoot", "provision_overshoot", "both_overshoot", "block_end_missing"])
def test_provision_source_repository_rejects_out_of_bounds_ranges(fault):
    catalog, unit, span = provision_catalog()
    if fault in {"block_overshoot", "both_overshoot"}:
        span.block_char_end += 10
    if fault in {"provision_overshoot", "both_overshoot"}:
        span.provision_char_end += 10
    if fault == "block_end_missing":
        span.block_char_end = None
    with pytest.raises(ContextScopeError):
        asyncio.run(catalog.provision_sources({unit.provision_id}))


@pytest.mark.parametrize("start,end", [(True, 20), (0, False), (-1, 20), (0, 10000), (5, 5)])
def test_invalid_quote_request_ranges_fail_before_opening_source_snapshot(start, end):
    verifier, _, unit = prepared_source()
    with pytest.raises(CitationVerificationError, match="quotation_range_invalid"):
        asyncio.run(verifier.verify(unit, start, end))
    assert verifier.context_repository.calls == []


def test_unrelated_proof_cannot_turn_into_a_claim_with_no_citation_indexes():
    decision, draft = setup()
    unit = decision.context.units[0]
    quote = unit.text[unit.text.index("Synthetic"):]
    claim = draft.claims[0].model_copy(update={"quote": quote, "claim_text": quote})
    with pytest.raises(CitationVerificationError):
        asyncio.run(CitationVerifier(FakeSource([proof_for(unit, end=3)])).verify(
            draft.model_copy(update={"claims": [claim]}), decision))


def test_incomplete_source_coverage_is_repaired_only_within_existing_limit():
    decision, draft = setup()
    unit = decision.context.units[0]
    generator = FakeGenerator([draft, draft])
    source = FakeSource([proof_for(unit, end=5)])
    result = asyncio.run(QuestionAnswerService(FakePrep(decision), generator, CitationVerifier(source),
        repair_attempts=1).ask(AskIn(question="What does the synthetic section say?")))
    assert result.status == "insufficient_evidence" and not result.claims and not result.citations
    assert len(generator.calls) == 2 and generator.calls[1][1] == "source_span_gap"


def test_pre_ai_fact_question_also_requires_complete_verified_condition_coverage():
    decision = fact_decision()
    unit = next(u for u in decision.context.units if u.evidence_id == decision.clarification_evidence_id)
    generator = FakeGenerator([])
    result = asyncio.run(QuestionAnswerService(FakePrep(decision), generator,
        CitationVerifier(FakeSource([proof_for(unit, end=5)]))).ask(AskIn(question="May I act under this section?")))
    assert result.status == "insufficient_evidence" and result.clarification_question is None
    assert not result.citations and generator.calls == []


def test_fresh_source_failure_is_a_service_error_with_no_generation_retry_or_private_details():
    class Unavailable(FakeSource):
        async def verify_many(self, requests):
            raise RuntimeError("PRIVATE-DB-ERROR")
    decision, draft = setup()
    generator = FakeGenerator([draft])
    result = asyncio.run(QuestionAnswerService(FakePrep(decision), generator, CitationVerifier(Unavailable())).ask(
        AskIn(question="What does the synthetic section say?")))
    assert result.status == "service_unavailable" and not result.citations
    assert "PRIVATE-DB-ERROR" not in result.model_dump_json() and len(generator.calls) == 1
