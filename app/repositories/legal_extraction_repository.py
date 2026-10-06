"""Durable page-extraction jobs and read models for the local inspector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import fitz
from sqlalchemy import and_, func, or_, select

from app.db.legal_corpus_models import (
    FileBlock,
    FileExtraction,
    FilePage,
    JobAttempt,
    LegalFile,
    LegalFileAssignment,
    LegalInstrument,
    LegalVersion,
    ProcessingJob,
)
from app.db.session import Database
from app.services.legal_extraction import ExtractedPage, ExtractionConfig, sha256_text


class StaleExtractionClaim(Exception):
    """Another attempt now owns this job."""


@dataclass(frozen=True, slots=True)
class ExtractionClaim:
    job_id: UUID
    attempt_id: UUID
    extraction_id: UUID
    file_id: UUID
    object_key: str
    sha256: str
    page_count: int


class LegalExtractionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def claim_next(
        self, *, worker_id: str, config: ExtractionConfig, lease_seconds: int = 3600
    ) -> ExtractionClaim | None:
        """Lock one queued or expired job, creating an auditable new attempt."""
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.task_type == "extract",
                    or_(
                        ProcessingJob.status == "queued",
                        and_(
                            ProcessingJob.status == "running",
                            or_(ProcessingJob.lease_until < now, ProcessingJob.lease_until.is_(None)),
                        ),
                    ),
                ).order_by(ProcessingJob.created_at).limit(1).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if job is None:
                return None

            if job.status == "running":
                previous = (await session.execute(
                    select(JobAttempt).where(JobAttempt.job_id == job.id)
                    .order_by(JobAttempt.attempt_number.desc()).limit(1)
                )).scalar_one_or_none()
                if previous and previous.status == "running":
                    previous.status = "failed"
                    previous.error_message = "Worker lease expired; a new attempt took over."
                    previous.completed_at = now
                previous_extraction = (await session.execute(
                    select(FileExtraction).where(
                        FileExtraction.file_id == job.target_file_id,
                        FileExtraction.processing_status == "running",
                    ).order_by(FileExtraction.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                if previous_extraction:
                    previous_extraction.processing_status = "failed"

            source = await session.get(LegalFile, job.target_file_id)
            if source is None:
                raise RuntimeError("An extraction job has no registered PDF.")
            attempt_number = (await session.scalar(
                select(func.coalesce(func.max(JobAttempt.attempt_number), 0))
                .where(JobAttempt.job_id == job.id)
            )) + 1
            attempt = JobAttempt(
                id=uuid4(), job_id=job.id, attempt_number=attempt_number,
                worker_id=worker_id, status="running", started_at=now, heartbeat_at=now,
            )
            extraction = FileExtraction(
                id=uuid4(), file_id=source.id, engine_name="pymupdf",
                engine_version=fitz.VersionBind, ocr_engine="tesseract",
                ocr_languages=config.languages, config_sha256=config.digest(),
                processing_status="running", started_at=now,
            )
            session.add_all((attempt, extraction))
            job.status = "running"
            job.started_at = now
            job.completed_at = None
            job.lease_until = now + timedelta(seconds=lease_seconds)
            job.progress_done = 0
            job.progress_total = source.page_count
            job.error_message = None
            await session.flush()
            return ExtractionClaim(
                job_id=job.id, attempt_id=attempt.id, extraction_id=extraction.id,
                file_id=source.id, object_key=source.object_key,
                sha256=source.sha256, page_count=source.page_count,
            )

    async def save_page(
        self, claim: ExtractionClaim, page: ExtractedPage, *, lease_seconds: int = 3600
    ) -> None:
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(
                select(ProcessingJob).where(ProcessingJob.id == claim.job_id).with_for_update()
            )).scalar_one()
            attempt = await session.get(JobAttempt, claim.attempt_id)
            if job.status != "running" or attempt is None or attempt.status != "running":
                raise StaleExtractionClaim("The extraction job is no longer owned by this attempt.")
            if page.page_index != job.progress_done + 1:
                raise ValueError("Pages must be persisted once, in physical page order.")
            record = FilePage(
                id=uuid4(), extraction_id=claim.extraction_id,
                page_index=page.page_index, width_pt=page.width_pt,
                height_pt=page.height_pt, rotation_deg=page.rotation_deg,
                raw_text=page.raw_text, normalized_text=page.normalized_text,
                text_sha256=sha256_text(page.normalized_text),
                extraction_method=page.extraction_method, outcome=page.outcome,
                ocr_confidence=page.ocr_confidence, warnings=page.warnings,
            )
            session.add(record)
            await session.flush()
            session.add_all(FileBlock(
                id=uuid4(), extraction_id=claim.extraction_id, page_id=record.id,
                block_ordinal=block.ordinal, block_kind=block.kind,
                raw_text=block.raw_text, normalized_text=block.normalized_text,
                text_sha256=sha256_text(block.normalized_text),
                page_char_start=block.page_char_start, page_char_end=block.page_char_end,
                bbox=block.bbox, warnings=block.warnings,
            ) for block in page.blocks)
            job.progress_done = page.page_index
            job.lease_until = now + timedelta(seconds=lease_seconds)
            attempt.heartbeat_at = now

    async def complete(self, claim: ExtractionClaim, output_sha256: str) -> None:
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(
                select(ProcessingJob).where(ProcessingJob.id == claim.job_id).with_for_update()
            )).scalar_one()
            attempt = await session.get(JobAttempt, claim.attempt_id)
            extraction = await session.get(FileExtraction, claim.extraction_id)
            count = await session.scalar(
                select(func.count(FilePage.id)).where(FilePage.extraction_id == claim.extraction_id)
            )
            if (job.status != "running" or attempt is None or attempt.status != "running"
                    or extraction is None or count != claim.page_count
                    or job.progress_done != claim.page_count):
                raise StaleExtractionClaim("The extraction is incomplete or its lease was superseded.")
            extraction.processing_status = "completed"
            extraction.output_sha256 = output_sha256
            extraction.completed_at = now
            attempt.status = "completed"
            attempt.heartbeat_at = now
            attempt.completed_at = now
            job.status = "completed"
            job.lease_until = None
            job.completed_at = now

    async def fail(self, claim: ExtractionClaim, message: str) -> None:
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(
                select(ProcessingJob).where(ProcessingJob.id == claim.job_id).with_for_update()
            )).scalar_one()
            attempt = await session.get(JobAttempt, claim.attempt_id)
            extraction = await session.get(FileExtraction, claim.extraction_id)
            if attempt is None or attempt.status != "running" or job.status != "running":
                return
            if extraction:
                extraction.processing_status = "failed"
            attempt.status = "failed"
            attempt.error_message = message[:2000]
            attempt.completed_at = now
            job.status = "failed"
            job.error_message = message[:2000]
            job.lease_until = None
            job.completed_at = now

    async def requeue(self, job_id: UUID) -> bool:
        async with self.db.session() as session, session.begin():
            job = (await session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.id == job_id, ProcessingJob.task_type == "extract"
                ).with_for_update()
            )).scalar_one_or_none()
            if job is None or job.status not in {"failed", "completed"}:
                return False
            job.status = "queued"
            job.progress_done = 0
            job.error_message = None
            job.lease_until = None
            job.started_at = None
            job.completed_at = None
            return True

    async def get_file(self, file_id: UUID) -> LegalFile | None:
        async with self.db.session() as session:
            return await session.get(LegalFile, file_id)

    async def overview(self, file_id: UUID) -> dict | None:
        async with self.db.session() as session:
            source = await session.get(LegalFile, file_id)
            if source is None:
                return None
            identity = (await session.execute(
                select(LegalInstrument.canonical_title, LegalVersion.edition_key)
                .join(LegalVersion, LegalVersion.instrument_id == LegalInstrument.id)
                .join(LegalFileAssignment, LegalFileAssignment.legal_version_id == LegalVersion.id)
                .where(LegalFileAssignment.file_id == file_id).limit(1)
            )).one_or_none()
            job = (await session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.target_file_id == file_id, ProcessingJob.task_type == "extract"
                ).order_by(ProcessingJob.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            extractions = (await session.execute(
                select(FileExtraction).where(FileExtraction.file_id == file_id)
                .order_by(FileExtraction.created_at.desc())
            )).scalars().all()
            summaries = []
            for extraction in extractions:
                counts = (await session.execute(
                    select(FilePage.outcome, func.count(FilePage.id))
                    .where(FilePage.extraction_id == extraction.id)
                    .group_by(FilePage.outcome)
                )).all()
                summaries.append({
                    "id": extraction.id,
                    "status": extraction.processing_status,
                    "engine_name": extraction.engine_name,
                    "engine_version": extraction.engine_version,
                    "ocr_languages": extraction.ocr_languages,
                    "config_sha256": extraction.config_sha256,
                    "output_sha256": extraction.output_sha256,
                    "page_count": sum(count for _, count in counts),
                    "outcomes": {outcome: count for outcome, count in counts},
                    "created_at": extraction.created_at,
                })
            return {
                "file_id": source.id,
                "title": identity[0] if identity else source.original_filename,
                "edition_key": identity[1] if identity else None,
                "original_filename": source.original_filename,
                "page_count": source.page_count,
                "source_url": source.source_url,
                "job": {
                    "id": job.id,
                    "status": job.status,
                    "progress_done": job.progress_done,
                    "progress_total": job.progress_total,
                    "error_message": job.error_message,
                } if job else None,
                "extractions": summaries,
            }

    async def pages(self, extraction_id: UUID) -> list[dict] | None:
        async with self.db.session() as session:
            if await session.get(FileExtraction, extraction_id) is None:
                return None
            rows = (await session.execute(
                select(FilePage).where(FilePage.extraction_id == extraction_id)
                .order_by(FilePage.page_index)
            )).scalars().all()
            block_counts = dict((await session.execute(
                select(FileBlock.page_id, func.count(FileBlock.id))
                .where(FileBlock.extraction_id == extraction_id)
                .group_by(FileBlock.page_id)
            )).all())
            return [{
                "page_index": row.page_index,
                "outcome": row.outcome,
                "extraction_method": row.extraction_method,
                "text_length": len(row.normalized_text),
                "block_count": block_counts.get(row.id, 0),
                "warnings": row.warnings,
            } for row in rows]

    async def page_detail(self, extraction_id: UUID, page_index: int) -> dict | None:
        async with self.db.session() as session:
            row = (await session.execute(
                select(FilePage).where(
                    FilePage.extraction_id == extraction_id, FilePage.page_index == page_index
                )
            )).scalar_one_or_none()
            if row is None:
                return None
            blocks = (await session.execute(
                select(FileBlock).where(FileBlock.page_id == row.id)
                .order_by(FileBlock.block_ordinal)
            )).scalars().all()
            return {
                "page_index": row.page_index,
                "width_pt": float(row.width_pt) if row.width_pt is not None else None,
                "height_pt": float(row.height_pt) if row.height_pt is not None else None,
                "rotation_deg": row.rotation_deg,
                "raw_text": row.raw_text,
                "normalized_text": row.normalized_text,
                "text_sha256": row.text_sha256,
                "extraction_method": row.extraction_method,
                "outcome": row.outcome,
                "ocr_confidence": float(row.ocr_confidence) if row.ocr_confidence is not None else None,
                "warnings": row.warnings,
                "blocks": [{
                    "id": block.id,
                    "ordinal": block.block_ordinal,
                    "kind": block.block_kind,
                    "raw_text": block.raw_text,
                    "normalized_text": block.normalized_text,
                    "page_char_start": block.page_char_start,
                    "page_char_end": block.page_char_end,
                    "bbox": block.bbox,
                    "warnings": block.warnings,
                } for block in blocks],
            }

    async def extraction_belongs_to_file(self, extraction_id: UUID, file_id: UUID) -> bool:
        async with self.db.session() as session:
            return (await session.scalar(
                select(FileExtraction.id).where(
                    FileExtraction.id == extraction_id, FileExtraction.file_id == file_id
                )
            )) is not None
