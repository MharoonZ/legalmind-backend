"""Responses from the local legal-PDF registration endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class LegalUploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    jurisdiction_id: UUID
    instrument_id: UUID
    legal_version_id: UUID
    file_id: UUID
    job_id: UUID
    canonical_title: str
    edition_key: str
    original_filename: str
    sha256: str
    page_count: int
    job_status: str
    already_registered: bool
    created_at: datetime
