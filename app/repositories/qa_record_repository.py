"""Optional Phase C-only audit writes, enabled after the local migration is applied."""
from uuid import uuid4

from app.db.qa_record_models import AnswerClaim, AnswerEvidence, QaRun, RetrievalHit


class QaRecordRepository:
    def __init__(self, db, model_name: str):
        self.db = db
        self.model_name = model_name

    async def record(self, request, response, decision):
        context = decision.context
        reranked = context.reranked if context else None
        rank_by_id = ({row.candidate.chunk_id: row.reranker_rank for row in reranked.candidates}
                      if reranked else {})
        async with self.db.session() as session, session.begin():
            run_id = uuid4()
            session.add(QaRun(
                id=run_id, question=request.question, publication_id=response.publication_id,
                outcome=response.status,
                model=self.model_name if decision.outcome == "ready_for_prompt" else None,
                reranker_model=reranked.model_name if reranked else None,
            ))
            if reranked:
                for candidate in reranked.retrieval.results:
                    session.add(RetrievalHit(
                        id=uuid4(), qa_run_id=run_id, chunk_id=candidate.chunk_id,
                        provision_id=candidate.root_provision_id,
                        retrieval_rank=candidate.rank, exact_rank=candidate.exact_rank,
                        keyword_rank=candidate.keyword_rank, vector_rank=candidate.vector_rank,
                        rrf_rank=candidate.rrf_rank, rrf_score=candidate.rrf_score,
                        reranker_rank=rank_by_id.get(candidate.chunk_id),
                        reranker_score=reranked.all_scores.get(candidate.chunk_id),
                    ))
            for ordinal, claim in enumerate(response.claims, 1):
                claim_id = uuid4()
                session.add(AnswerClaim(id=claim_id, qa_run_id=run_id, ordinal=ordinal,
                                        text=claim.text, evidence_id=claim.evidence_id))
                for citation_index in claim.citation_indexes:
                    citation = response.citations[citation_index]
                    session.add(AnswerEvidence(
                        id=uuid4(), answer_claim_id=claim_id,
                        evidence_id=citation.evidence_id, chunk_id=citation.chunk_id,
                        provision_id=citation.provision_id,
                        legal_version_id=citation.legal_version_id,
                        revision_id=citation.revision_id,
                        source_span_id=citation.source_span_id,
                        file_id=citation.file_id, page_index=citation.page_index,
                        quote=citation.quote,
                    ))
