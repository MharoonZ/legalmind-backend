"""Step 12 evidence units and explicit context omissions."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.qa_evidence import RerankResult


class ContextUnit(BaseModel):
    model_config = ConfigDict(frozen=True)
    evidence_id: str
    source_kind: Literal["chunk", "provision"]
    relation: Literal["retrieved", "sibling", "definition", "reference", "qualification", "parent"]
    publication_id: UUID
    instrument_id: UUID
    legal_version_id: UUID
    revision_id: UUID
    chunk_set_id: UUID
    chunk_id: UUID | None = None
    provision_id: UUID
    instrument_title: str
    edition_key: str
    jurisdiction_code: str
    applicability_status: str
    kind: str
    label: str | None = None
    heading: str | None = None
    reference_path: str | None = None
    text: str
    source_refs: list[dict] = Field(default_factory=list)
    retrieval_rank: int | None = None
    reranker_rank: int | None = None
    reranker_score: float | None = Field(default=None, allow_inf_nan=False)
    ancestor_labels: tuple[str, ...] = ()


class ContextIssue(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: Literal[
        "candidate_missing", "scope_mismatch", "root_fragment_missing", "budget_omission",
        "source_missing", "unresolved_reference", "reference_target_missing",
        "qualification_missing", "parent_context_missing", "definition_heuristic",
        "reference_cycle", "reference_depth_limit", "optional_context_omitted",
    ]
    detail: str
    critical: bool = False
    source_id: str | None = None


class ContextReference(BaseModel):
    """An included dependency; structural lookups do not mark DB rows reviewed."""
    model_config = ConfigDict(frozen=True)
    source_provision_id: UUID
    target_instrument_id: UUID
    target_provision_id: UUID
    target_reference_path: str
    raw_reference: str
    method: Literal["stored", "same_revision_lookup"]
    source_char_start: int | None = None
    source_char_end: int | None = None


class ContextResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    reranked: RerankResult
    units: tuple[ContextUnit, ...] = ()
    issues: tuple[ContextIssue, ...] = ()
    reference_links: tuple[ContextReference, ...] = ()
    token_count: int = 0
    char_count: int = 0
    token_limit: int
    char_limit: int
    unit_limit: int
