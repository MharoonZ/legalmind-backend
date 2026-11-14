"""First-stage evidence retrieval, without answer generation."""
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class LegalReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["section", "rule", "article", "schedule"] = "section"
    label: str = Field(min_length=1, max_length=100)
    reference_path: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def valid_label(self):
        pattern = (r"(?:[ivxlcdm]+|\d+[a-z]*)" if self.kind == "schedule" else
                   r"\d+(?:-?[a-z]+)?(?:\([a-z0-9]+\))*")
        if not re.fullmatch(pattern, self.label, re.I):
            raise ValueError("Use a numbered legal label such as 19 or 19(2)(a)")
        return self


class SearchContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    previous_query: str | None = Field(default=None, min_length=1, max_length=4000)
    instrument_id: UUID | None = None
    law: str | None = Field(default=None, min_length=1, max_length=255)


class LegalSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=10, ge=1, le=50)
    instrument_id: UUID | None = None
    law: str | None = Field(default=None, min_length=1, max_length=255)
    jurisdiction_code: str | None = Field(default=None, min_length=1, max_length=50)
    reference: LegalReferenceIn | None = None
    context: SearchContextIn | None = None
    # Only /ask can opt in to its explicit follow-up semantics.
    _explicit_followup: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def bounded_followup(self):
        previous = self.context.previous_query if self.context else None
        if previous and len(previous) + 1 + len(self.query) > 4000:
            raise ValueError("The query and previous question together must fit within 4000 characters")
        return self

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        if not any(c.isalnum() for c in value):
            raise ValueError("Query must contain letters or numbers")
        return value


class CandidateOut(BaseModel):
    chunk_id: UUID
    chunk_set_id: UUID
    revision_id: UUID
    root_provision_id: UUID
    instrument_id: UUID
    legal_version_id: UUID
    instrument_title: str
    citation_key: str
    edition_key: str
    jurisdiction_code: str
    applicability_status: str
    kind: str
    label: str | None
    heading: str | None
    reference_path: str
    matched_reference_paths: list[str] = Field(default_factory=list)
    sequence_in_root: int
    root_total_chunks: int
    chunk_text: str
    source_refs: list[dict]
    rank: int
    exact_rank: int | None = None
    keyword_rank: int | None = None
    vector_rank: int | None = None
    rrf_rank: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    rrf_score: float = 0


class LegalSearchOut(BaseModel):
    status: Literal["ok", "no_results", "needs_clarification", "no_active_publication", "service_unavailable"]
    query: str
    retrieval_query: str
    publication_id: UUID | None = None
    publication_name: str | None = None
    context_used: bool = False
    message: str | None = None
    clarification_options: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_references: list[str] = Field(default_factory=list)
    results: list[CandidateOut] = Field(default_factory=list)
