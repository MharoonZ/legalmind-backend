"""Contract for POST /ask.

Agreed shape (Sprint 1, shared rule 1):
    answer, quote, section, page, evidence_id, supported: true | false

Shimza's frontend types must match this file.
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

    @model_validator(mode="after")
    def check_evidence_matches_supported(self) -> Self:
        evidence = (self.quote, self.section, self.page, self.evidence_id)
        if self.supported and any(v is None for v in evidence):
            raise ValueError(
                "A supported answer needs quote, section, page and evidence_id."
            )
        if not self.supported and any(v is not None for v in evidence):
            raise ValueError(
                "An unsupported answer must not include quote, section, page or evidence_id."
            )
        return self
