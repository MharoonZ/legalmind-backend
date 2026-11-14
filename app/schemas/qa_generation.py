"""Structured untrusted model draft; verified response uses separate schemas."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    quote: str
    claim_text: str


class DraftAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["explanation", "application"]
    text: str = Field(min_length=1, max_length=1500)
    claim_indexes: list[int] = Field(min_length=1, max_length=12)
    fact_quotes: list[str] = Field(default_factory=list, max_length=6)


class AnalysisReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported_indexes: list[int]
    complete: bool
    reason: str = Field(max_length=1000)


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["answered", "partial", "clarification_needed", "insufficient_evidence"]
    answer: str
    claims: list[DraftClaim]
    missing_support: str = Field(max_length=1000)
    clarification_question: str | None = Field(default=None, max_length=500)
    analysis: list[DraftAnalysis] = Field(default_factory=list, max_length=8)
