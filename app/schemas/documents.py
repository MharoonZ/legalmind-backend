"""Contracts for POST/GET /contracts (uploading a contract PDF).

This covers the *upload* stage only (Sprint 2, Abdullah's Week 1 scope):
save the file, validate it, create document + version records. A fresh
upload always comes back with status="uploaded" - clause extraction
(Haroon) and the AI summary (Week 2) are separate pieces of work that will
move a version through the later statuses below.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DocumentStatus = Literal[
    "uploaded",
    "extracting",
    "extracted",
    "extraction_failed",
    "summarizing",
    "summarized",
    "summary_failed",
]


class DocumentVersionOut(BaseModel):
    document_id: str = Field(..., description="Stable id of the uploaded file.")
    version_id: str = Field(..., description="Id of this processing attempt.")
    version_number: int = Field(..., description="1 for the first upload of this document.")
    status: DocumentStatus = Field(..., description="Where this version is in the pipeline.")
    filename: str = Field(..., description="Original filename as uploaded.")
    content_type: str
    size_bytes: int
    page_count: int
    question: str | None = Field(
        None,
        description=(
            "The chat question submitted with this upload, if any. Not "
            "answerable yet - extraction and the AI summary step run later."
        ),
    )
    error_message: str | None = Field(
        None, description="Set only if a later processing stage failed."
    )
    created_at: datetime
    message: str = Field(..., description="Human-readable note on what happens next.")
