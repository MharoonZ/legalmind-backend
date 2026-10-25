"""Step 13 decisions before any answer-generation service is available."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.qa_context import ContextResult


class NecessaryFact(BaseModel):
    """An explicit requirement from a reviewed rule, not inferred from a score."""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    key: str = Field(min_length=1)
    question: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    quote: str = Field(min_length=20)


class FactAssessment(BaseModel):
    """Trusted server-side rule assessment; never accepted from /ask input."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    necessary_facts: tuple[NecessaryFact, ...] = ()
    known_fact_keys: frozenset[str] = frozenset()


class EvidenceDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome: Literal["ready_for_prompt", "clarification_needed", "insufficient_evidence", "service_unavailable"]
    reason: str
    evidence_ids: tuple[str, ...] = ()
    clarification_question: str | None = None
    clarification_options: tuple[dict, ...] = ()
    clarification_fact: NecessaryFact | None = None
    clarification_evidence_id: str | None = None
    heuristic_notes: tuple[str, ...] = ()
    context: ContextResult | None = None
