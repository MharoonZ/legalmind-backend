"""POST /ask contract. Citations are assembled by the server, never by the model."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.legal_search import SearchContextIn


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)
    law: str | None = Field(default=None, min_length=1, max_length=255)
    context: SearchContextIn | None = None
    # Earlier turns, oldest first. The server uses the most recent ones.
    history: list[ConversationTurn] = Field(default_factory=list, max_length=50)

    @field_validator("question")
    @classmethod
    def meaningful_question(cls, value):
        if not any(char.isalnum() for char in value):
            raise ValueError("Question must contain letters or numbers")
        return value


class VerifiedCitation(BaseModel):
    """One quoted passage, located in the published text and its source PDF page."""
    model_config = ConfigDict(frozen=True)
    evidence_id: str
    publication_id: UUID
    legal_version_id: UUID
    revision_id: UUID
    provision_id: UUID
    chunk_id: UUID | None = None
    law: str
    edition: str
    jurisdiction: str
    kind: str
    label: str | None
    reference_path: str | None
    quote: str
    source_span_id: UUID
    file_id: UUID
    file_sha256: str
    filename: str
    page_index: int
    source_url: str | None = None


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    evidence_id: str
    citation_indexes: tuple[int, ...]


class AskOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["answered", "partial", "clarification_needed", "insufficient_evidence", "service_unavailable"]
    question: str
    # Markers such as [1] in the answer refer to citations[0].
    answer: str
    publication_id: UUID | None = None
    claims: tuple[VerifiedClaim, ...] = ()
    citations: tuple[VerifiedCitation, ...] = ()
    clarification_question: str | None = None
    clarification_options: tuple[dict, ...] = ()
    limitations: tuple[str, ...] = ()
    missing_support: str | None = None
