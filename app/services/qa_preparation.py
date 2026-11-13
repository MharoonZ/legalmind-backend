"""Compose retrieval and Steps 11 through 13; no answer-generation client is called."""
from __future__ import annotations
import logging
from time import perf_counter
from typing import Protocol

from app.repositories.qa_context_repository import ContextScopeError
from app.schemas.qa_decision import EvidenceDecision, FactAssessment
from app.services.qa_context import ContextTokenizationError
from app.services.qa_evidence_gate import relevance_by_root, supports_context_retry
from app.services.qa_reranker import EvidenceContractError, RerankerUnavailable

logger = logging.getLogger(__name__)


class FactRequirementProvider(Protocol):
    """Reviewed rules only; implementations must not call an LLM to invent rules."""
    async def assess(self, request, context) -> FactAssessment: ...


class QuestionPreparationService:
    def __init__(self, retrieval_service, reranker, context_expander, evidence_gate, *,
                 fact_provider: FactRequirementProvider | None = None):
        self.retrieval_service = retrieval_service
        self.reranker = reranker
        self.context_expander = context_expander
        self.evidence_gate = evidence_gate
        self.fact_provider = fact_provider

    async def prepare(self, request, *, necessary_facts=(), known_fact_keys=frozenset()):
        try:
            response = await self.retrieval_service.search(request)
            started = perf_counter()
            reranked = await self.reranker.rerank(response)
            signals = relevance_by_root(reranked)
            if not reranked.candidates and response._has_semantic_alternative:
                retry = request.model_copy()
                retry._prefer_semantic_alternative = True
                alternative = await self.retrieval_service.search(retry)
                if alternative.status == "ok" and alternative.publication_id == response.publication_id:
                    reranked_alternative = await self.reranker.rerank(alternative)
                    if reranked_alternative.candidates:
                        reranked = reranked_alternative
                        logger.info("qa_alternative_retrieval accepted_candidates=%d", len(reranked.candidates))
            # Keep top candidates based on vector embeddings and reranker score.
            # Retain all top reranked candidates so the LLM gets full context.
            retained = tuple(reranked.candidates[:self.reranker.settings.top_k])
            logger.info("qa_rerank retrieved=%d ranked=%d retained=%d elapsed_ms=%.0f", len(response.results),
                        len(reranked.candidates), len(retained), (perf_counter() - started) * 1000)
            reranked = reranked.model_copy(update={"candidates": retained})
            started = perf_counter()
            context = await self.context_expander.expand(reranked)
            if (len(signals) > 1 and "exact" not in signals.values()
                    and any(issue.critical for issue in context.issues)):
                # A complete top root can support a partial answer even when
                # another potential source has unavailable dependencies.
                root = retained[0].candidate.root_provision_id
                top = tuple(row for row in retained if row.candidate.root_provision_id == root)
                if supports_context_retry(reranked, top):
                    narrow = await self.context_expander.expand(reranked.model_copy(update={"candidates": top}))
                    if narrow.units and not any(issue.critical for issue in narrow.issues):
                        context = narrow.model_copy(update={"coverage_limited": True})
                        logger.info("qa_context_retained_complete_root omitted_roots=%d", len(signals) - 1)
            logger.info("qa_context passages=%d tokens=%d issues=%s elapsed_ms=%.0f",
                        len(context.units), context.token_count,
                        sorted({issue.code for issue in context.issues}), (perf_counter() - started) * 1000)
        except (ContextScopeError, EvidenceContractError) as exc:
            logger.warning("qa_preparation_failed error=%s", type(exc).__name__)
            return EvidenceDecision(outcome="service_unavailable",
                                    reason="The published source identity or evidence contract is invalid.")
        except (ContextTokenizationError, RerankerUnavailable) as exc:
            logger.warning("qa_preparation_failed error=%s", type(exc).__name__)
            return EvidenceDecision(outcome="service_unavailable",
                                    reason="The local evidence models could not prepare this request.")
        decision = self.evidence_gate.decide(context, necessary_facts=necessary_facts,
                                             known_fact_keys=known_fact_keys)
        logger.info("qa_evidence_gate outcome=%s evidence=%d missing_references=%d",
                    decision.outcome, len(decision.evidence_ids), len(decision.missing_references))
        if (decision.outcome != "ready_for_prompt" or decision.missing_references or
                decision.limited_source_context or self.fact_provider is None):
            return decision
        try:
            assessment = FactAssessment.model_validate(await self.fact_provider.assess(request, context))
        except Exception:
            return EvidenceDecision(outcome="service_unavailable", context=context,
                                    reason="The reviewed fact requirements could not be checked.")
        return self.evidence_gate.decide(context,
            necessary_facts=(*necessary_facts, *assessment.necessary_facts),
            known_fact_keys=known_fact_keys | assessment.known_fact_keys)
