"""Contract for POST /ask.

Sprint 1 shape (kept exactly, so Shimza's frontend keeps working):
    answer, quote, section, page, evidence_id, supported

Sprint 2 additions, all optional for the client:
    citations  - every supporting passage, not only the first one
    meta       - retrieval diagnostics, filled only when LEGALMIND_DEBUG=true

`quote`, `section`, `page` and `evidence_id` always mirror `citations[0]`.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(
        ...,
        min_length=3,
        max_length=500,
        description="The user's question about the Contract Act 1872 (sections 1-30).",
        examples=["What makes an agreement a contract?"],
    )

    @model_validator(mode="after")
    def reject_content_free_question(self) -> Self:
        """Block input that cannot be searched, e.g. "???" or "....."."""
        if not any(char.isalnum() for char in self.question):
            raise ValueError("Question must contain letters or numbers.")
        return self


class Citation(BaseModel):
    """One passage that supports the answer. Verified against the corpus."""

    evidence_id: str = Field(..., description="Immutable passage ID from law_passages.")
    section: int = Field(..., description="Section number of the Act.")
    section_title: str = Field(..., description="Marginal title of that section.")
    page: int = Field(..., description="PDF page where the quote appears.")
    quote: str = Field(..., description="Text copied from the stored passage.")
    law_name: str = Field(..., description="Name of the statute.")
    jurisdiction: str = Field(..., description="Country the statute belongs to.")
    source_url: str = Field(..., description="Public URL of the source PDF.")


class AskMeta(BaseModel):
    """Retrieval diagnostics. Null unless debug mode is on."""

    strategy: str = Field(..., description="direct_section | hybrid | keyword_only")
    retrievers: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    top_keyword_score: float | None = None
    top_vector_score: float | None = None
    embedding_model: str | None = None
    llm_model: str | None = None


class AskResponse(BaseModel):
    supported: bool = Field(
        ..., description="True if the answer is backed by a stored passage."
    )
    answer: str = Field(..., description="Short English answer, or a refusal message.")
    quote: str | None = Field(None, description="Exact text from the stored passage.")
    section: int | None = Field(None, description="Section number of the Act.")
    page: int | None = Field(None, description="PDF page where the quote appears.")
    evidence_id: str | None = Field(
        None, description="Immutable evidence ID from the retrieval layer."
    )
    citations: list[Citation] = Field(
        default_factory=list, description="All verified supporting passages."
    )
    meta: AskMeta | None = Field(None, description="Debug-only retrieval details.")

    @model_validator(mode="after")
    def check_evidence_matches_supported(self) -> Self:
        evidence = (self.quote, self.section, self.page, self.evidence_id)
        if self.supported:
            if any(v is None for v in evidence):
                raise ValueError(
                    "A supported answer needs quote, section, page and evidence_id."
                )
            if not self.citations:
                raise ValueError("A supported answer needs at least one citation.")
            first = self.citations[0]
            if (first.evidence_id, first.section, first.page, first.quote) != (
                self.evidence_id,
                self.section,
                self.page,
                self.quote,
            ):
                raise ValueError("Flat evidence fields must mirror citations[0].")
        else:
            if any(v is not None for v in evidence):
                raise ValueError(
                    "An unsupported answer must not include quote, section, page or evidence_id."
                )
            if self.citations:
                raise ValueError("An unsupported answer must not carry citations.")
        return self

    @classmethod
    def unsupported(cls, message: str, meta: AskMeta | None = None) -> Self:
        return cls(supported=False, answer=message, citations=[], meta=meta)
