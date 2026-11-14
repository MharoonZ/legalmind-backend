"""Claims and citations after server-side source verification."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class VerifiedCitation(BaseModel):
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


class VerifiedAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["explanation", "application"]
    text: str
    claim_indexes: tuple[int, ...]


class VerifiedDraft(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome: Literal["answered", "partial", "clarification_needed", "insufficient_evidence"]
    claims: tuple[VerifiedClaim, ...]
    citations: tuple[VerifiedCitation, ...]
    missing_support: str
    clarification_question: str | None = None
    analysis: tuple[VerifiedAnalysis, ...] = ()
