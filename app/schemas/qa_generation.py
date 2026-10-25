"""Structured untrusted model draft; verified response uses separate schemas."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    quote: str
    claim_text: str


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["answered", "partial", "clarification_needed", "insufficient_evidence"]
    answer: str
    claims: list[DraftClaim]
    missing_support: str = Field(max_length=1000)
    clarification_question: str | None = Field(default=None, max_length=500)
