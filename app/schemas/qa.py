"""POST /ask contract; citations are resolved by the server, never the model."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.legal_search import SearchContextIn
from app.schemas.qa_verified import VerifiedCitation, VerifiedClaim


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=3, max_length=4000)
    law: str | None = Field(default=None, min_length=1, max_length=255)
    context: SearchContextIn | None = None

    @field_validator("question")
    @classmethod
    def meaningful_question(cls, value):
        if not any(char.isalnum() for char in value):
            raise ValueError("Question must contain letters or numbers")
        return value

    def search_query(self) -> str:
        if self.context and self.context.previous_query:
            return f"{self.context.previous_query}\nFollow-up information: {self.question}"
        return self.question

    @model_validator(mode="after")
    def bounded_followup(self):
        if len(self.search_query()) > 4000:
            raise ValueError("The question and previous question together must fit within 4000 characters")
        return self


class AskOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["answered", "partial", "clarification_needed", "insufficient_evidence", "service_unavailable"]
    question: str
    answer: str
    publication_id: UUID | None = None
    claims: tuple[VerifiedClaim, ...] = ()
    citations: tuple[VerifiedCitation, ...] = ()
    clarification_question: str | None = None
    clarification_options: tuple[dict, ...] = ()
    limitations: tuple[str, ...] = ()
    missing_support: str | None = None
