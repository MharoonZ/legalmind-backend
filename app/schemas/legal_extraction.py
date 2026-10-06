"""Responses for the local page-extraction inspector."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ExtractionWarningOut(BaseModel):
    code: str
    message: str
    severity: str


class ExtractionJobOut(BaseModel):
    id: UUID
    status: str
    progress_done: int
    progress_total: int | None
    error_message: str | None


class ExtractionSummaryOut(BaseModel):
    id: UUID
    status: str
    engine_name: str
    engine_version: str
    ocr_languages: str | None
    config_sha256: str
    output_sha256: str | None
    page_count: int
    outcomes: dict[str, int]
    created_at: datetime


class FileOverviewOut(BaseModel):
    file_id: UUID
    title: str
    edition_key: str | None
    original_filename: str
    page_count: int
    source_url: str | None
    job: ExtractionJobOut | None
    extractions: list[ExtractionSummaryOut]


class FilePageSummaryOut(BaseModel):
    page_index: int
    outcome: str
    extraction_method: str
    text_length: int
    block_count: int
    warnings: list[ExtractionWarningOut]


class FileBlockOut(BaseModel):
    id: UUID
    ordinal: int
    kind: str
    raw_text: str
    normalized_text: str
    page_char_start: int | None
    page_char_end: int | None
    bbox: dict[str, float] | None
    warnings: list[ExtractionWarningOut]


class FilePageDetailOut(BaseModel):
    page_index: int
    width_pt: float | None
    height_pt: float | None
    rotation_deg: int | None
    raw_text: str
    normalized_text: str
    text_sha256: str | None
    extraction_method: str
    outcome: str
    ocr_confidence: float | None
    warnings: list[ExtractionWarningOut]
    blocks: list[FileBlockOut]
