"""Transactional registration of legal PDFs before extraction begins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import String, and_, cast, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import LegalUploadConflictError
from app.db.legal_corpus_models import (
    Category,
    InstrumentCategory,
    Jurisdiction,
    LegalFile,
    LegalFileAssignment,
    LegalInstrument,
    LegalVersion,
    ProcessingJob,
)
from app.services.legal_upload import StoredLegalPdf


@dataclass(frozen=True, slots=True)
class LegalRegistration:
    jurisdiction_code: str
    jurisdiction_name: str
    category_slug: str
    category_name: str
    instrument_type: str
    canonical_title: str
    citation_key: str
    edition_key: str
    assignment_role: str = "primary"
    publication_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    date_source: str | None = None
    publisher: str | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class LegalUploadRecord:
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


def _same_text(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


class LegalIngestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register_upload(
        self, metadata: LegalRegistration, pdf: StoredLegalPdf
    ) -> LegalUploadRecord:
        """Get/create the registry chain, source assignment, and one extract job.

Unique constraints and INSERT .. ON CONFLICT make repeated/concurrent uploads
idempotent. Conflicting metadata returns 409 rather than silently changing a
previously registered Act or legal edition.
"""
        async with self.session.begin():
            await self.session.execute(
                insert(Jurisdiction).values(
                    id=uuid4(), code=metadata.jurisdiction_code,
                    name=metadata.jurisdiction_name,
                ).on_conflict_do_nothing(index_elements=[Jurisdiction.code])
            )
            jurisdiction = (await self.session.execute(
                select(Jurisdiction).where(Jurisdiction.code == metadata.jurisdiction_code)
            )).scalar_one()
            if not _same_text(jurisdiction.name, metadata.jurisdiction_name):
                raise LegalUploadConflictError("Jurisdiction code already has a different name.")

            await self.session.execute(
                insert(Category).values(
                    id=uuid4(), slug=metadata.category_slug,
                    name=metadata.category_name,
                ).on_conflict_do_nothing(index_elements=[Category.slug])
            )
            category = (await self.session.execute(
                select(Category).where(Category.slug == metadata.category_slug)
            )).scalar_one()
            if not _same_text(category.name, metadata.category_name):
                raise LegalUploadConflictError("Category slug already has a different name.")

            await self.session.execute(
                insert(LegalInstrument).values(
                    id=uuid4(), jurisdiction_id=jurisdiction.id,
                    instrument_type=metadata.instrument_type,
                    canonical_title=metadata.canonical_title,
                    citation_key=metadata.citation_key,
                ).on_conflict_do_nothing(
                    index_elements=[LegalInstrument.jurisdiction_id, LegalInstrument.citation_key]
                )
            )
            instrument = (await self.session.execute(
                select(LegalInstrument).where(
                    LegalInstrument.jurisdiction_id == jurisdiction.id,
                    LegalInstrument.citation_key == metadata.citation_key,
                )
            )).scalar_one()
            if instrument.instrument_type != metadata.instrument_type or not _same_text(
                instrument.canonical_title, metadata.canonical_title
            ):
                raise LegalUploadConflictError("Citation key already identifies a different Act or Rules.")

            await self.session.execute(
                insert(InstrumentCategory).values(
                    instrument_id=instrument.id, category_id=category.id
                ).on_conflict_do_nothing(
                    index_elements=[InstrumentCategory.instrument_id, InstrumentCategory.category_id]
                )
            )

            await self.session.execute(
                insert(LegalVersion).values(
                    id=uuid4(), instrument_id=instrument.id,
                    edition_key=metadata.edition_key,
                    publication_date=metadata.publication_date,
                    effective_from=metadata.effective_from,
                    effective_to=metadata.effective_to,
                    applicability_status="unverified" if any((
                        metadata.publication_date, metadata.effective_from, metadata.effective_to
                    )) else "unknown",
                    date_source=metadata.date_source,
                ).on_conflict_do_nothing(
                    index_elements=[LegalVersion.instrument_id, LegalVersion.edition_key]
                )
            )
            version = (await self.session.execute(
                select(LegalVersion).where(
                    LegalVersion.instrument_id == instrument.id,
                    LegalVersion.edition_key == metadata.edition_key,
                )
            )).scalar_one()
            for field in ("publication_date", "effective_from", "effective_to"):
                provided = getattr(metadata, field)
                if provided is not None and getattr(version, field) != provided:
                    raise LegalUploadConflictError(
                        f"Edition key already has a different {field.replace('_', ' ')}."
                    )

            await self.session.execute(
                insert(LegalFile).values(
                    id=uuid4(), original_filename=pdf.original_filename,
                    object_key=pdf.object_key, sha256=pdf.sha256,
                    byte_size=pdf.byte_size, media_type="application/pdf",
                    page_count=pdf.page_count, publisher=metadata.publisher,
                    source_url=metadata.source_url,
                ).on_conflict_do_nothing()
            )
            legal_file = (await self.session.execute(
                select(LegalFile).where(LegalFile.sha256 == pdf.sha256)
            )).scalar_one_or_none()
            if legal_file is None or legal_file.object_key != pdf.object_key or (
                legal_file.byte_size != pdf.byte_size or legal_file.page_count != pdf.page_count
            ):
                raise LegalUploadConflictError("Stored PDF metadata conflicts with an existing record.")

            created_assignment = (await self.session.execute(
                insert(LegalFileAssignment).values(
                    file_id=legal_file.id, legal_version_id=version.id,
                    assignment_role=metadata.assignment_role,
                ).on_conflict_do_nothing(
                    index_elements=[LegalFileAssignment.file_id, LegalFileAssignment.legal_version_id]
                ).returning(LegalFileAssignment.file_id)
            )).scalar_one_or_none()
            if created_assignment is None:
                assignment = (await self.session.execute(
                    select(LegalFileAssignment).where(
                        LegalFileAssignment.file_id == legal_file.id,
                        LegalFileAssignment.legal_version_id == version.id,
                    )
                )).scalar_one()
                if assignment.assignment_role != metadata.assignment_role:
                    raise LegalUploadConflictError("PDF is already assigned to this edition with a different role.")

            job_key = f"extract:{legal_file.id}"
            await self.session.execute(
                insert(ProcessingJob).values(
                    id=uuid4(), task_type="extract", target_file_id=legal_file.id,
                    idempotency_key=job_key, status="queued",
                    progress_total=pdf.page_count,
                ).on_conflict_do_nothing(
                    index_elements=[ProcessingJob.task_type, ProcessingJob.idempotency_key]
                )
            )
            job = (await self.session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.task_type == "extract",
                    ProcessingJob.idempotency_key == job_key,
                )
            )).scalar_one()

            return LegalUploadRecord(
                jurisdiction_id=jurisdiction.id,
                instrument_id=instrument.id,
                legal_version_id=version.id,
                file_id=legal_file.id,
                job_id=job.id,
                canonical_title=instrument.canonical_title,
                edition_key=version.edition_key,
                original_filename=legal_file.original_filename,
                sha256=legal_file.sha256,
                page_count=legal_file.page_count,
                job_status=job.status,
                already_registered=created_assignment is None,
                created_at=legal_file.created_at,
            )

    async def recent_uploads(self, *, limit: int = 25, offset: int = 0) -> list[LegalUploadRecord]:
        """List recent assignments, including a file's extraction job state."""
        statement = (
            select(LegalFileAssignment, LegalFile, LegalVersion, LegalInstrument, ProcessingJob)
            .join(LegalFile, LegalFile.id == LegalFileAssignment.file_id)
            .join(LegalVersion, LegalVersion.id == LegalFileAssignment.legal_version_id)
            .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
            .join(ProcessingJob, and_(
                ProcessingJob.target_file_id == LegalFile.id,
                ProcessingJob.task_type == "extract",
                ProcessingJob.idempotency_key == (literal("extract:") + cast(LegalFile.id, String)),
            ))
            .order_by(LegalFileAssignment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(statement)).all()
        return [LegalUploadRecord(
            jurisdiction_id=instrument.jurisdiction_id,
            instrument_id=instrument.id,
            legal_version_id=version.id,
            file_id=legal_file.id,
            job_id=job.id,
            canonical_title=instrument.canonical_title,
            edition_key=version.edition_key,
            original_filename=legal_file.original_filename,
            sha256=legal_file.sha256,
            page_count=legal_file.page_count,
            job_status=job.status,
            already_registered=True,
            created_at=legal_file.created_at,
        ) for _, legal_file, version, instrument, job in rows]
