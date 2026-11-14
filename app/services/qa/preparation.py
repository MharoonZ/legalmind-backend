"""Compose retrieval, reranking and complete source context."""
import logging
from time import perf_counter

from app.repositories.qa_context_repository import ContextScopeError
from app.schemas.legal_search import LegalSearchIn
from app.schemas.qa_decision import PreparedQuestion
from app.services.qa.context import ContextTokenizationError
from app.services.qa.context_validation import validate_context
from app.services.qa.reranker import EvidenceContractError, RerankerUnavailable

logger = logging.getLogger(__name__)


class QuestionPreparationService:
    def __init__(self, retrieval_service, reranker, context_expander):
        self.retrieval_service = retrieval_service
        self.reranker = reranker
        self.context_expander = context_expander

    async def prepare(self, request: LegalSearchIn) -> PreparedQuestion:
        response = await self.retrieval_service.search(request)
        if response.status != "ok":
            outcome = {"needs_clarification": "clarification_needed",
                       "no_results": "insufficient_evidence"}.get(response.status, "service_unavailable")
            return PreparedQuestion(outcome=outcome, publication_id=response.publication_id,
                reason=response.message or "Published search is unavailable.",
                clarification_question=response.message if outcome == "clarification_needed" else None,
                clarification_options=tuple(response.clarification_options))
        started = perf_counter()
        try:
            ranked = await self.reranker.rerank(response)
            logger.info("qa_rerank candidates=%d elapsed_ms=%.0f", len(ranked.candidates),
                        (perf_counter() - started) * 1000)
            started = perf_counter()
            expand = getattr(self.context_expander, "expand_packed", self.context_expander.expand)
            context = await expand(ranked)
            if not context.units or any(issue.critical for issue in context.issues):
                return PreparedQuestion(outcome="insufficient_evidence", context=context,
                    reason="Required source passages or dependencies could not fit the complete context.")
            validate_context(context)
        except (ContextScopeError, ContextTokenizationError, EvidenceContractError,
                RerankerUnavailable, ValueError) as exc:
            logger.warning("qa_preparation_failed error=%s", type(exc).__name__)
            return PreparedQuestion(outcome="service_unavailable", publication_id=response.publication_id,
                reason="Published source context could not be prepared.")
        logger.info("qa_context units=%d tokens=%d limited=%s elapsed_ms=%.0f",
                    len(context.units), context.token_count, context.coverage_limited,
                    (perf_counter() - started) * 1000)
        return PreparedQuestion(outcome="ready_for_prompt", context=context,
            reason="Published source context prepared.",
            evidence_ids=tuple(unit.evidence_id for unit in context.units),
            missing_references=tuple(response.missing_references),
            limited_source_context=context.coverage_limited)
