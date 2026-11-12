"""Phase C evidence wrappers and cross-encoder rankings."""
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.legal_search import CandidateOut, LegalSearchOut


class RerankedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    publication_id: UUID
    candidate: CandidateOut
    reranker_score: float | None = Field(allow_inf_nan=False)
    reranker_rank: int = Field(ge=1)


class RerankResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    retrieval: LegalSearchOut
    model_name: str
    candidates: tuple[RerankedCandidate, ...] = ()
    # Retain every score for later retrieval-hit recording, separately from RRF.
    all_scores: dict[UUID, float] = Field(default_factory=dict)
    dropped_chunk_ids: tuple[UUID, ...] = ()
    warnings: tuple[str, ...] = ()
