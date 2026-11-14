"""Step 16 uses synthetic quoted passages and a fake PDF-source checker."""
import asyncio

import pytest

from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa.citations import CitationVerificationError, CitationVerifier
from tests.qa_fixtures import prepared_context
from app.services.qa.source_verification import SourceProof, covered_proofs
from tests.qa_fixtures import identity
from tests.test_qa_context import expand, graph


class FakeSource:
    def __init__(self, proofs=None):
        self.calls = []
        self.proofs = proofs

    async def verify_many(self, requests):
        return [await self.verify(*request) for request in requests]

    async def verify(self, unit, start, end):
        self.calls.append((unit.evidence_id, start, end))
        return self.proofs if self.proofs is not None else [SourceProof(
            start=start, end=end, provision_id=unit.provision_id,
            kind=unit.kind, label=unit.label, reference_path=unit.reference_path,
            source_span_id=identity("span-1"), file_id=identity("file"),
            file_sha256="a" * 64, filename="synthetic.pdf", page_index=1,
            source_url=None)]


def setup():
    catalog, reranked = graph()
    decision = prepared_context(expand(catalog, reranked))
    unit = decision.context.units[0]
    quote = unit.text
    draft = AnswerDraft(outcome="answered", answer="Untrusted model prose.",
                        claims=[DraftClaim(evidence_id=unit.evidence_id,
                                           quote=quote, claim_text=quote)],
                        missing_support="")
    return decision, draft


def test_exact_quotation_resolves_real_metadata_and_model_answer_is_not_trusted():
    decision, draft = setup()
    verified = asyncio.run(CitationVerifier(FakeSource()).verify(draft, decision))
    assert verified.outcome == "answered"
    assert verified.claims[0].text == draft.claims[0].quote
    assert verified.citations[0].page_index == 1
    assert verified.citations[0].source_url is None
    assert "Untrusted model prose" not in str(verified)


def test_subsection_lookup_cannot_be_answered_by_only_quoting_a_sibling():
    catalog, reranked = graph()
    original = reranked.candidates[0]
    wanted = original.candidate.reference_path + "/ss:2"
    row = original.model_copy(update={"candidate": original.candidate.model_copy(
        update={"exact_rank": 1, "matched_reference_paths": [wanted]})})
    decision = prepared_context(expand(catalog, reranked.model_copy(update={"candidates": (row,)})))
    unit = decision.context.units[0]
    draft = AnswerDraft(outcome="answered", answer="", missing_support="",
        claims=[DraftClaim(evidence_id=unit.evidence_id, quote=unit.text, claim_text=unit.text)])
    with pytest.raises(CitationVerificationError, match="requested_reference_not_cited"):
        asyncio.run(CitationVerifier(FakeSource()).verify(draft, decision))


def test_omitted_whole_candidates_do_not_override_a_supported_answer():
    decision, draft = setup()
    limited = prepared_context(decision.context.model_copy(update={"coverage_limited": True}))
    source = FakeSource()
    complete = asyncio.run(CitationVerifier(source).verify(draft, limited))
    assert complete.outcome == "answered" and complete.citations
    verified = asyncio.run(CitationVerifier(source).verify(
        draft.model_copy(update={"outcome": "partial", "missing_support": "The requested remedy is not established."}), limited))
    assert verified.outcome == "partial" and verified.citations
    assert "remedy" in verified.missing_support


def test_all_claims_are_sent_to_source_checker_in_one_batch():
    decision, draft = setup()
    class BatchSource(FakeSource):
        batches = 0

        async def verify_many(self, requests):
            self.batches += 1
            return await super().verify_many(requests)
    source = BatchSource()
    verified = asyncio.run(CitationVerifier(source).verify(
        draft.model_copy(update={"claims": draft.claims * 5}), decision))
    assert len(verified.claims) == 5
    assert source.batches == 1
    assert len(verified.citations) == 1


def test_invalid_later_claim_is_rejected_before_any_source_read():
    decision, draft = setup()
    source = FakeSource()
    invalid = draft.claims[0].model_copy(update={"evidence_id": "invented"})
    with pytest.raises(CitationVerificationError, match="invalid_evidence_id"):
        asyncio.run(CitationVerifier(source).verify(
            draft.model_copy(update={"claims": [draft.claims[0], invalid]}), decision))
    assert source.calls == []


def test_quote_across_pdf_pages_has_page_specific_citation_text():
    decision, draft = setup()
    unit = decision.context.units[0]
    split = len(unit.text) // 2
    proofs = [SourceProof(
        start=start, end=end, provision_id=unit.provision_id,
        kind=unit.kind, label=unit.label, reference_path=unit.reference_path,
        source_span_id=identity(f"span-{page}"), file_id=identity("file"),
        file_sha256="a" * 64, filename="synthetic.pdf", page_index=page,
        source_url=None) for page, start, end in ((1, 0, split), (2, split, len(unit.text)))]
    verified = asyncio.run(CitationVerifier(FakeSource(proofs)).verify(draft, decision))
    assert [citation.page_index for citation in verified.citations] == [1, 2]
    assert verified.citations[0].quote == unit.text[:split].strip()
    assert verified.citations[1].quote == unit.text[split:].strip()
    assert verified.claims[0].citation_indexes == (0, 1)


@pytest.mark.parametrize("change,code", [
    ({"evidence_id": "chunk:invented"}, "invalid_evidence_id"),
    ({"quote": "A fabricated quotation that is sufficiently long.",
      "claim_text": "A fabricated quotation that is sufficiently long."}, "quotation_mismatch"),
    ({"claim_text": "A different legal claim with unsupported words."}, "unsupported_claim"),
])
def test_invalid_id_quote_or_claim_is_rejected(change, code):
    decision, draft = setup()
    claim = draft.claims[0].model_copy(update=change)
    with pytest.raises(CitationVerificationError) as failure:
        asyncio.run(CitationVerifier(FakeSource()).verify(
            draft.model_copy(update={"claims": [claim]}), decision))
    assert failure.value.code == code


def test_wrong_publication_and_version_are_rejected():
    decision, draft = setup()
    context = decision.context
    wrong_pub = context.units[0].model_copy(update={"publication_id": identity("wrong-publication")})
    altered = context.model_copy(update={"units": (wrong_pub, *context.units[1:])})
    with pytest.raises(CitationVerificationError, match="wrong_publication"):
        asyncio.run(CitationVerifier(FakeSource()).verify(
            draft, decision.model_copy(update={"context": altered})))
    wrong_version = context.units[0].model_copy(update={"revision_id": identity("wrong-revision")})
    altered = context.model_copy(update={"units": (wrong_version, *context.units[1:])})
    with pytest.raises(CitationVerificationError, match="wrong_source_version"):
        asyncio.run(CitationVerifier(FakeSource()).verify(
            draft, decision.model_copy(update={"context": altered})))


def test_missing_source_proof_and_unmapped_characters_are_rejected():
    decision, draft = setup()
    with pytest.raises(CitationVerificationError, match="source_span_missing"):
        asyncio.run(CitationVerifier(FakeSource([])).verify(draft, decision))
    partial = SourceProof(start=0, end=8, provision_id=identity("provision-1"),
                          kind="section", label="1", reference_path="f:test/s:1",
                          source_span_id=identity("span-1"), file_id=identity("file"),
                          file_sha256="a" * 64, filename="synthetic.pdf", page_index=1,
                          source_url=None)
    with pytest.raises(CitationVerificationError, match="source_span_gap"):
        covered_proofs(draft.claims[0].quote, 0, len(draft.claims[0].quote), [partial])


def test_insufficient_draft_must_not_smuggle_claims():
    decision, draft = setup()
    with pytest.raises(CitationVerificationError, match="unsupported_claim"):
        asyncio.run(CitationVerifier(FakeSource()).verify(
            draft.model_copy(update={"outcome": "insufficient_evidence"}), decision))


@pytest.mark.parametrize("changes,code", [
    ({"outcome": "clarification_needed", "missing_support": "Permit status."}, "missing_clarification"),
    ({"outcome": "clarification_needed", "clarification_question": "Do you have a permit?"}, "missing_clarification"),
    ({"clarification_question": "Do you have a permit?"}, "unexpected_clarification"),
    ({"outcome": "partial"}, "missing_limitation"),
    ({"missing_support": "The location is unknown."}, "inconsistent_outcome"),
])
def test_inconsistent_or_incomplete_outcomes_are_rejected(changes, code):
    decision, draft = setup()
    with pytest.raises(CitationVerificationError, match=code):
        asyncio.run(CitationVerifier(FakeSource()).verify(draft.model_copy(update=changes), decision))
