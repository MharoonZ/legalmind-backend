"""Prepared request state passed from retrieval to generation."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from app.schemas.qa_context import ContextResult


class PreparedQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome: Literal["ready_for_prompt", "clarification_needed", "insufficient_evidence", "service_unavailable"]
    reason: str
    publication_id: UUID | None = None
    evidence_ids: tuple[str, ...] = ()
    missing_references: tuple[str, ...] = ()
    limited_source_context: bool = False
    clarification_question: str | None = None
    clarification_options: tuple[dict, ...] = ()
    context: ContextResult | None = None
