"""Source identity regressions independent of semantic relevance."""
import pytest
from app.services.qa.context_validation import validate_context
from tests.qa_fixtures import prepared_context, candidate, identity, PUBLICATION
from tests.test_qa_context import expand, graph
from app.schemas.qa_evidence import RerankResult, RerankedCandidate
from tests.qa_fixtures import retrieval

def lexical_context(sources, *, query="payroll notice"):
    catalog, original = graph()
    base = expand(catalog, original)
    unit = base.units[0]
    units = tuple(unit.model_copy(update={
        "evidence_id": f"chunk:{s.chunk_id}", "chunk_id": s.chunk_id,
        "provision_id": s.root_provision_id, "text": s.chunk_text,
        "source_refs": s.source_refs, "instrument_id": s.instrument_id,
        "legal_version_id": s.legal_version_id, "revision_id": s.revision_id,
        "chunk_set_id": s.chunk_set_id,
    }) for s in sources)
    reranked = RerankResult(retrieval=retrieval(sources, query=query, retrieval_query=query),
        model_name="synthetic", candidates=tuple(RerankedCandidate(publication_id=PUBLICATION,
            candidate=s, reranker_score=-1, reranker_rank=i) for i, s in enumerate(sources, 1)))
    return base.model_copy(update={"units": units, "reranked": reranked})


@pytest.mark.parametrize("fault", ["span", "file", "block", "hash", "page_zero", "page_boolean", "approximate"])
def test_source_identity_is_checked_before_ai_eligibility(fault):
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    unit = context.units[0]
    ref = dict(unit.source_refs[0])
    if fault in {"span", "file", "block"}:
        ref[{"span": "source_span_id", "file": "file_id", "block": "block_id"}[fault]] = "missing-id"
    elif fault == "hash":
        ref["file_sha256"] = "not-a-hash"
    elif fault.startswith("page"):
        ref["page_index"] = False if fault == "page_boolean" else 0
    else:
        ref["mapping_quality"] = "approximate"
    context = context.model_copy(update={"units": (unit.model_copy(update={"source_refs": [ref]}), *context.units[1:])})
    assert prepared_context(context).outcome == "insufficient_evidence"


@pytest.mark.parametrize("fault", ["publication", "version", "revision", "chunk_set", "text", "root", "evidence_id", "duplicate"])
def test_context_identity_cannot_be_detached_from_retained_candidates(fault):
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    unit = context.units[0]
    fields = {"publication": "publication_id", "version": "legal_version_id", "revision": "revision_id",
              "chunk_set": "chunk_set_id", "root": "provision_id"}
    if fault in fields:
        changed = unit.model_copy(update={fields[fault]: identity("wrong-identity")})
    elif fault == "text":
        changed = unit.model_copy(update={"text": "A different unselected source passage."})
    elif fault == "evidence_id":
        changed = unit.model_copy(update={"evidence_id": "chunk:invented"})
    else:
        changed = unit
    units = (changed, *context.units[1:]) + ((unit,) if fault == "duplicate" else ())
    assert prepared_context(context.model_copy(update={"units": units})).outcome == "insufficient_evidence"


def test_conflicting_version_in_a_supplemental_passage_is_not_ignored():
    catalog, reranked = graph(qualifier=True)
    context = expand(catalog, reranked)
    units = tuple(u.model_copy(update={"revision_id": identity("other-version")})
                  if u.relation == "qualification" else u for u in context.units)
    assert prepared_context(context.model_copy(update={"units": units})).outcome == "insufficient_evidence"


def test_unretained_exact_hit_cannot_approve_a_different_retained_passage():
    catalog, reranked = graph()
    context = expand(catalog, reranked)
    extra = RerankedCandidate(publication_id=PUBLICATION, candidate=candidate(2, exact_rank=1),
                              reranker_score=10, reranker_rank=2)
    changed = context.reranked.model_copy(update={"candidates": (*context.reranked.candidates, extra)})
    assert prepared_context(context.model_copy(update={"reranked": changed})).outcome == "insufficient_evidence"


def test_zero_exact_rank_does_not_bypass_the_source_contract():
    source = candidate(1, exact_rank=0)
    assert prepared_context(lexical_context([source])).outcome == "insufficient_evidence"
