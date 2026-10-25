"""Read-only inspector queries. No extraction, registration or review writes."""
from uuid import UUID

from sqlalchemy import select

from app.db.legal_corpus_models import (
    FileBlock, FileExtraction, FilePage, JobAttempt, LegalFile, LegalFileAssignment,
    LegalInstrument, LegalVersion, ProcessingJob,
)


def page_summary(page):
    return {"page_index": page.page_index, "method": page.extraction_method, "outcome": page.outcome,
            "characters": len(page.normalized_text), "warnings": page.warnings,
            "width_pt": float(page.width_pt) if page.width_pt else None,
            "height_pt": float(page.height_pt) if page.height_pt else None,
            "rotation_deg": page.rotation_deg, "ocr_confidence": None if page.ocr_confidence is None else float(page.ocr_confidence)}


class LegalInspectionRepository:
    def __init__(self, session):
        self.session = session

    async def source(self, file_id):
        file = await self.session.get(LegalFile, file_id)
        if file is None:
            return None
        return {"object_key": file.object_key, "sha256": file.sha256,
                "byte_size": file.byte_size, "page_count": file.page_count}

    async def documents(self, limit=25, offset=0):
        files = (await self.session.scalars(select(LegalFile).order_by(LegalFile.created_at.desc(), LegalFile.id)
                                           .limit(limit).offset(offset))).all()
        return [await self.document(file.id) for file in files]

    async def document(self, file_id):
        file = await self.session.get(LegalFile, file_id)
        if file is None:
            return None
        titles = (await self.session.scalars(select(LegalInstrument.canonical_title)
            .join(LegalVersion, LegalVersion.instrument_id == LegalInstrument.id)
            .join(LegalFileAssignment, LegalFileAssignment.legal_version_id == LegalVersion.id)
            .where(LegalFileAssignment.file_id == file_id))).all()
        jobs = (await self.session.scalars(select(ProcessingJob).where(
            ProcessingJob.target_file_id == file_id, ProcessingJob.task_type == "extract")
            .order_by(ProcessingJob.created_at.desc()))).all()
        extractions = (await self.session.scalars(select(FileExtraction).where(FileExtraction.file_id == file_id)
            .order_by(FileExtraction.created_at.desc(), FileExtraction.id))).all()
        result = {"file_id": str(file.id), "filename": file.original_filename, "titles": list(titles),
                  "registered_page_count": file.page_count, "sha256": file.sha256,
                  "jobs": [{"id": str(j.id), "status": j.status, "progress_done": j.progress_done,
                            "progress_total": j.progress_total, "error": j.error_message} for j in jobs],
                  "extractions": [{"id": str(e.id), "status": e.processing_status,
                                   "created_at": e.created_at.isoformat(), "engine": e.engine_name,
                                   "engine_version": e.engine_version, "ocr_engine": e.ocr_engine,
                                   "ocr_languages": e.ocr_languages, "config_sha256": e.config_sha256,
                                   "output_sha256": e.output_sha256} for e in extractions]}
        return result

    async def extraction(self, extraction_id):
        extraction = await self.session.get(FileExtraction, extraction_id)
        if extraction is None:
            return None
        file = await self.session.get(LegalFile, extraction.file_id)
        pages = list((await self.session.scalars(select(FilePage).where(FilePage.extraction_id == extraction_id)
                                                .order_by(FilePage.page_index))).all())
        indices = {p.page_index for p in pages}
        expected = set(range(1, file.page_count + 1))
        return {"id": str(extraction.id), "file_id": str(file.id), "status": extraction.processing_status,
                "engine": extraction.engine_name, "engine_version": extraction.engine_version,
                "ocr_engine": extraction.ocr_engine, "ocr_languages": extraction.ocr_languages,
                "config_sha256": extraction.config_sha256, "output_sha256": extraction.output_sha256,
                "total": file.page_count, "processed": len(pages),
                "failed": sum(p.outcome == "failed" for p in pages),
                "needs_review": sum(p.outcome == "needs_review" for p in pages),
                "with_text": sum(bool(p.normalized_text.strip()) for p in pages),
                "missing_pages": sorted(expected - indices), "extra_pages": sorted(indices - expected),
                "coverage_complete": indices == expected and extraction.processing_status == "completed" and bool(extraction.output_sha256),
                "human_review": "not_evaluated", "pages": [page_summary(p) for p in pages]}

    async def page(self, extraction_id, page_index):
        page = (await self.session.scalars(select(FilePage).where(
            FilePage.extraction_id == extraction_id, FilePage.page_index == page_index))).one_or_none()
        if page is None:
            return None
        blocks = (await self.session.scalars(select(FileBlock).where(FileBlock.page_id == page.id)
                                             .order_by(FileBlock.block_ordinal))).all()
        return {**page_summary(page), "id": str(page.id), "extraction_id": str(extraction_id),
                "raw_text": page.raw_text, "normalized_text": page.normalized_text, "text_sha256": page.text_sha256,
                "blocks": [{"ordinal": b.block_ordinal, "kind": b.block_kind,
                            "raw_text": b.raw_text, "text": b.normalized_text, "bbox": b.bbox,
                            "start": b.page_char_start, "end": b.page_char_end, "warnings": b.warnings} for b in blocks]}

    async def job(self, job_id: UUID):
        job = await self.session.get(ProcessingJob, job_id)
        if job is None or job.task_type != "extract":
            return None
        attempts = (await self.session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)
                                               .order_by(JobAttempt.attempt_number))).all()
        return {"id": str(job.id), "file_id": str(job.target_file_id), "status": job.status,
                "progress_done": job.progress_done, "progress_total": job.progress_total,
                "error": job.error_message, "started_at": job.started_at, "completed_at": job.completed_at,
                "lease_until": job.lease_until,
                "attempts": [{"number": a.attempt_number, "status": a.status, "started_at": a.started_at,
                              "completed_at": a.completed_at, "heartbeat_at": a.heartbeat_at,
                              "error": a.error_message} for a in attempts]}
