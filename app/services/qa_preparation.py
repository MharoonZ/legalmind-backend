"""Compose retrieval and Steps 11 through 13; no answer-generation client is called."""
from __future__ import annotations
from typing import Protocol

from app.repositories.qa_context_repository import ContextScopeError
from app.schemas.qa_decision import EvidenceDecision, FactAssessment
from app.services.qa_context import ContextTokenizationError
from app.services.qa_reranker import EvidenceContractError, RerankerUnavailable


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
            reranked = await self.reranker.rerank(response)
            context = await self.context_expander.expand(reranked)
        except (ContextScopeError, EvidenceContractError):
            return EvidenceDecision(outcome="service_unavailable",
                                    reason="The published source identity or evidence contract is invalid.")
        except (ContextTokenizationError, RerankerUnavailable):
            return EvidenceDecision(outcome="service_unavailable",
                                    reason="The local evidence models could not prepare this request.")
        decision = self.evidence_gate.decide(context, necessary_facts=necessary_facts,
                                             known_fact_keys=known_fact_keys)
        if decision.outcome != "ready_for_prompt" or self.fact_provider is None:
            return decision
        try:
            assessment = FactAssessment.model_validate(await self.fact_provider.assess(request, context))
        except Exception:
            return EvidenceDecision(outcome="service_unavailable", context=context,
                                    reason="The reviewed fact requirements could not be checked.")
        return self.evidence_gate.decide(context,
            necessary_facts=(*necessary_facts, *assessment.necessary_facts),
            known_fact_keys=known_fact_keys | assessment.known_fact_keys)
