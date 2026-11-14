"""Synthetic evidence only; these fixtures are not legal advice or live corpus data."""
import hashlib
from uuid import NAMESPACE_URL, uuid5

from app.schemas.legal_search import CandidateOut, LegalSearchOut


def identity(name):
    return uuid5(NAMESPACE_URL, "legalmind-synthetic-qa/" + name)


PUBLICATION = identity("publication")
INSTRUMENT = identity("instrument")
REVISION = identity("revision")
VERSION = identity("version")
CHUNK_SET = identity("chunk-set")


def candidate(index=1, **changes):
    content = f"Section {index}. Synthetic source words for an isolated test."
    digest = hashlib.sha256(content.encode()).hexdigest()
    provision_id = identity(f"provision-{index}")
    values = dict(
        chunk_id=identity(f"chunk-{index}"), chunk_set_id=CHUNK_SET,
        revision_id=REVISION, root_provision_id=provision_id,
        instrument_id=INSTRUMENT, legal_version_id=VERSION,
        instrument_title="Synthetic Test Act", citation_key="synthetic-test",
        edition_key="test-edition", jurisdiction_code="TEST",
        applicability_status="unknown", kind="section", label=str(index),
        heading="Synthetic provision", reference_path=f"f:test/s:{index}",
        sequence_in_root=1, root_total_chunks=1, chunk_text=content,
        rank=index, keyword_rank=index, vector_rank=index, rrf_rank=index,
        keyword_score=.2, vector_score=.8, rrf_score=1 / (60 + index),
        source_refs=[dict(
            provision_id=provision_id, source_span_id=identity(f"span-{index}"),
            extraction_id=identity("extraction"), file_id=identity("file"),
            file_sha256="a" * 64, filename="synthetic.pdf", source_url=None,
            publisher=None, reference_path=f"f:test/s:{index}", kind="section",
            label=str(index), mapping_quality="exact", page_id=identity(f"page-{index}"),
            page_index=index, block_id=identity(f"block-{index}"),
            block_text_sha256=digest, provision_text_sha256=digest,
            provision_char_start=0, provision_char_end=len(content),
            chunk_char_start=0, chunk_char_end=len(content),
        )],
    )
    values.update(changes)
    return CandidateOut(**values)


def retrieval(candidates=None, **changes):
    values = dict(status="ok", query="What does the synthetic source say?",
        retrieval_query="What does the synthetic source say?", publication_id=PUBLICATION,
        publication_name="synthetic-test-publication",
        results=candidates if candidates is not None else [candidate(i) for i in range(1, 11)])
    values.update(changes)
    return LegalSearchOut(**values)


class FakeScorer:
    """Controlled model scores; no real cross-encoder or network call."""
    def __init__(self, scores=None, error=None):
        self.scores = scores
        self.error = error
        self.calls = []
    def score_pairs(self, pairs):
        self.calls.append(pairs)
        if self.error:
            raise self.error
        return self.scores if self.scores is not None else [float(i) for i in range(len(pairs))]



def prepared_context(context):
    """Build a prepared fixture without a relevance classifier."""
    from app.schemas.qa_decision import PreparedQuestion
    from app.services.qa.context_validation import validate_context
    response=context.reranked.retrieval
    outcome={"no_results":"insufficient_evidence", "needs_clarification":"clarification_needed",
             "no_active_publication":"service_unavailable", "service_unavailable":"service_unavailable"}.get(
                 response.status, "ready_for_prompt")
    try:
        if outcome == "ready_for_prompt":
            validate_context(context)
    except ValueError:
        outcome="insufficient_evidence"
    return PreparedQuestion(outcome=outcome, context=context,
        reason=response.message or "Synthetic source context.",
        evidence_ids=tuple(unit.evidence_id for unit in context.units),
        missing_references=tuple(response.missing_references),
        limited_source_context=context.coverage_limited,
        clarification_question=response.message if outcome=="clarification_needed" else None)
