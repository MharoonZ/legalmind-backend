"""Optional audit trail of answered questions (``QA_RECORD_RUNS=true``).

Writes only the four Phase C tables created by migrations 20261017_18 and
20261111_19. A failure here never affects the answer already produced.
"""
from __future__ import annotations

from uuid import uuid4

from app.db.qa_record_models import AnswerClaim, AnswerEvidence, QaRun, RetrievalHit
from app.retrieval.service import RetrievalResult
from app.schemas.qa import AskIn, AskOut


class QaRecordRepository:
    def __init__(self, db):
        self.db = db

    async def record(self, request: AskIn, response: AskOut, retrieval: RetrievalResult,
                     model_name: str | None, reranker_model: str | None) -> None:
        generated = response.status in {"answered", "partial", "clarification_needed", "insufficient_evidence"}
        async with self.db.session() as session, session.begin():
            run_id = uuid4()
            session.add(QaRun(
                id=run_id, question=request.question, publication_id=response.publication_id,
                outcome=response.status, model=model_name if generated else None,
                reranker_model=reranker_model))
            for position, candidate in enumerate(retrieval.candidates, 1):
                session.add(RetrievalHit(
                    id=uuid4(), qa_run_id=run_id, chunk_id=candidate.chunk.id,
                    provision_id=candidate.chunk.root_provision_id, retrieval_rank=candidate.rank or position,
                    exact_rank=candidate.exact_rank, keyword_rank=candidate.keyword_rank,
                    vector_rank=candidate.vector_rank, rrf_rank=candidate.rrf_rank,
                    rrf_score=candidate.rrf_score,
                    reranker_rank=position if candidate.reranker_score is not None else None,
                    reranker_score=candidate.reranker_score))
            for ordinal, claim in enumerate(response.claims, 1):
                claim_id = uuid4()
                session.add(AnswerClaim(id=claim_id, qa_run_id=run_id, ordinal=ordinal,
                                        text=claim.text, evidence_id=claim.evidence_id))
                for index in claim.citation_indexes:
                    citation = response.citations[index]
                    session.add(AnswerEvidence(
                        id=uuid4(), answer_claim_id=claim_id, evidence_id=citation.evidence_id,
                        chunk_id=citation.chunk_id, provision_id=citation.provision_id,
                        legal_version_id=citation.legal_version_id, revision_id=citation.revision_id,
                        source_span_id=citation.source_span_id, file_id=citation.file_id,
                        page_index=citation.page_index, quote=citation.quote))
