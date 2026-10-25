"""Durable, fenced extraction writes using Haroon's existing tables only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, func, or_, select, update

from app.db.legal_corpus_models import FileBlock, FileExtraction, FilePage, JobAttempt, LegalFile, ProcessingJob
from app.services.legal_pdf_extraction import content_hash


class LeaseLost(Exception):
    pass


class JobConflict(Exception):
    pass


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    attempt_id: UUID
    file_id: UUID
    generation: datetime


def claim_statement(now, job_id=None):
    statement = select(ProcessingJob).where(
        ProcessingJob.task_type == "extract", ProcessingJob.target_file_id.is_not(None),
        or_(ProcessingJob.status == "queued", and_(ProcessingJob.status == "running",
            or_(ProcessingJob.lease_until.is_(None), ProcessingJob.lease_until <= now))),
    )
    if job_id is not None:
        statement = statement.where(ProcessingJob.id == job_id)
    return statement.order_by(ProcessingJob.created_at, ProcessingJob.id).limit(1).with_for_update(skip_locked=True)


class LegalExtractionRepository:
    def __init__(self, session_factory, *, lease_seconds=120, max_attempts=3):
        self.sessions = session_factory
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def _now(self, session):
        # Read wall time after acquiring the row lock, not transaction-start
        # time (which can predate a lock wait and incorrectly extend a lease).
        return utc((await session.execute(select(func.clock_timestamp()))).scalar_one())

    async def claim(self, worker_id: str, job_id: UUID | None = None) -> Claim | None:
        async with self.sessions() as session, session.begin():
            now = await self._now(session)
            job = (await session.execute(claim_statement(now, job_id))).scalar_one_or_none()
            if job is None:
                return None
            attempts = list((await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)
                                                   .order_by(JobAttempt.attempt_number))).all())
            for previous in attempts:
                if previous.status == "running":
                    previous.status, previous.completed_at, previous.error_message = "failed", now, "lease_expired"
            current = [a for a in attempts if job.started_at and utc(a.started_at) >= utc(job.started_at)]
            if len(current) >= self.max_attempts:
                job.status, job.error_message, job.completed_at, job.lease_until = "failed", "attempt_limit_reached", now, None
                await session.execute(update(FileExtraction).where(
                    FileExtraction.file_id == job.target_file_id,
                    FileExtraction.started_at == job.started_at,
                    FileExtraction.processing_status == "running",
                ).values(processing_status="failed"))
                return None
            if job.started_at is None:
                job.started_at = now
            attempt = JobAttempt(id=uuid4(), job_id=job.id,
                                 attempt_number=max((a.attempt_number for a in attempts), default=0) + 1,
                                 worker_id=worker_id[:200], status="running", started_at=now, heartbeat_at=now)
            session.add(attempt)
            job.status, job.completed_at, job.error_message = "running", None, None
            job.lease_until = now + timedelta(seconds=self.lease_seconds)
            return Claim(job.id, attempt.id, job.target_file_id, utc(job.started_at))

    async def _owned(self, session, claim: Claim):
        job = (await session.execute(select(ProcessingJob).where(ProcessingJob.id == claim.job_id)
                                     .with_for_update())).scalar_one()
        now = await self._now(session)
        attempt = await session.get(JobAttempt, claim.attempt_id)
        if (job.status != "running" or not job.lease_until or utc(job.lease_until) <= now
                or attempt is None or attempt.status != "running"
                or utc(job.started_at) != claim.generation):
            raise LeaseLost("This worker no longer owns the job")
        return job, attempt, now

    async def heartbeat(self, claim: Claim):
        async with self.sessions() as session, session.begin():
            job, attempt, now = await self._owned(session, claim)
            attempt.heartbeat_at = now
            job.lease_until = now + timedelta(seconds=self.lease_seconds)

    async def prepare(self, claim: Claim, config: dict):
        async with self.sessions() as session, session.begin():
            job, _, _ = await self._owned(session, claim)
            source = await session.get(LegalFile, claim.file_id)
            if source is None:
                raise JobConflict("source_record_missing")
            digest = content_hash(config)
            extraction_id = uuid5(job.id, claim.generation.isoformat() + ":" + digest)
            await session.execute(update(FileExtraction).where(
                FileExtraction.file_id == claim.file_id,
                FileExtraction.started_at == claim.generation,
                FileExtraction.id != extraction_id,
                FileExtraction.processing_status == "running",
            ).values(processing_status="failed"))
            extraction = await session.get(FileExtraction, extraction_id)
            if extraction is None:
                extraction = FileExtraction(id=extraction_id, file_id=source.id,
                    engine_name="PyMuPDF", engine_version=config["engine_version"],
                    ocr_engine=(config["ocr"]["engine_version"] or "Tesseract unavailable") if config["ocr_enabled"] else None,
                    ocr_languages=config["languages"] if config["ocr_enabled"] else None,
                    config_sha256=digest, processing_status="running", started_at=claim.generation)
                session.add(extraction)
            elif extraction.processing_status != "completed":
                extraction.processing_status = "running"
            source_data = {"object_key": source.object_key, "sha256": source.sha256,
                           "byte_size": source.byte_size, "page_count": source.page_count}
            return extraction_id, source_data

    async def coverage(self, extraction_id: UUID) -> set[int]:
        async with self.sessions() as session:
            return set((await session.scalars(select(FilePage.page_index).where(FilePage.extraction_id == extraction_id))).all())

    async def set_total(self, claim, total, extraction_id):
        async with self.sessions() as session, session.begin():
            job, _, _ = await self._owned(session, claim)
            job.progress_total = total
            job.progress_done = (await session.execute(select(func.count()).select_from(FilePage)
                .where(FilePage.extraction_id == extraction_id))).scalar_one()

    async def save_page(self, claim, extraction_id, result):
        async with self.sessions() as session, session.begin():
            job, _, _ = await self._owned(session, claim)
            extraction = await session.get(FileExtraction, extraction_id)
            if extraction is None or extraction.file_id != claim.file_id or utc(extraction.started_at) != claim.generation:
                raise JobConflict("wrong_extraction")
            if not 1 <= result["page_index"] <= job.progress_total:
                raise JobConflict("page_out_of_range")
            if hashlib.sha256(result["normalized_text"].encode()).hexdigest() != result["text_sha256"]:
                raise JobConflict("page_text_hash_mismatch")
            for ordinal, block in enumerate(result["blocks"], 1):
                start, end = block["page_char_start"], block["page_char_end"]
                if block["block_ordinal"] != ordinal:
                    raise JobConflict("invalid_block_order")
                if hashlib.sha256(block["normalized_text"].encode()).hexdigest() != block["text_sha256"]:
                    raise JobConflict("block_text_hash_mismatch")
                if block["normalized_text"]:
                    if (start is None or end is None or not 0 <= start < end <= len(result["normalized_text"])
                            or result["normalized_text"][start:end] != block["normalized_text"]):
                        raise JobConflict("invalid_block_offsets")
                elif start is not None or end is not None:
                    raise JobConflict("invalid_block_offsets")
            page_id = uuid5(extraction_id, str(result["page_index"]))
            existing = await session.get(FilePage, page_id)
            if existing is None:
                if extraction.processing_status == "completed":
                    raise JobConflict("completed_extraction_is_immutable")
                fields = {k: v for k, v in result.items() if k != "blocks"}
                page = FilePage(id=page_id, extraction_id=extraction_id, **fields)
                session.add(page)
                await session.flush()
                for block in result["blocks"]:
                    session.add(FileBlock(id=uuid5(page_id, str(block["block_ordinal"])),
                        extraction_id=extraction_id, page_id=page_id, **block))
                await session.flush()
            job.progress_done = (await session.execute(select(func.count()).select_from(FilePage)
                .where(FilePage.extraction_id == extraction_id))).scalar_one()

    async def finish(self, claim, extraction_id, physical_count):
        async with self.sessions() as session, session.begin():
            job, attempt, now = await self._owned(session, claim)
            extraction = await session.get(FileExtraction, extraction_id)
            if (extraction is None or extraction.file_id != claim.file_id
                    or utc(extraction.started_at) != claim.generation):
                raise JobConflict("wrong_extraction")
            if physical_count < 1 or job.progress_total != physical_count:
                raise JobConflict("invalid_physical_count")
            pages = list((await session.scalars(select(FilePage).where(FilePage.extraction_id == extraction_id)
                                                .order_by(FilePage.page_index))).all())
            if [p.page_index for p in pages] != list(range(1, physical_count + 1)):
                raise JobConflict("incomplete_page_coverage")
            output = []
            for page in pages:
                blocks = list((await session.scalars(select(FileBlock).where(FileBlock.page_id == page.id)
                                                     .order_by(FileBlock.block_ordinal))).all())
                data = {key: getattr(page, key) for key in ("page_index", "raw_text", "normalized_text", "text_sha256",
                    "printed_label", "rotation_deg", "extraction_method", "outcome", "warnings")}
                data.update(width_pt=float(page.width_pt) if page.width_pt else None,
                            height_pt=float(page.height_pt) if page.height_pt else None,
                            ocr_confidence=float(page.ocr_confidence) if page.ocr_confidence is not None else None)
                data["blocks"] = [{key: getattr(b, key) for key in ("block_ordinal", "block_kind", "raw_text",
                    "normalized_text", "text_sha256", "page_char_start", "page_char_end", "bbox", "warnings")} for b in blocks]
                output.append(data)
            extraction.processing_status, extraction.completed_at = "completed", now
            extraction.output_sha256 = content_hash(output)
            job.progress_done = job.progress_total = physical_count
            job.status, job.completed_at, job.lease_until = "completed", now, None
            attempt.status, attempt.completed_at, attempt.heartbeat_at = "completed", now, now
            # Completion means every page has an outcome, including failures.
            return {"job_id": str(job.id), "extraction_id": str(extraction_id), "processing_status": "completed",
                    "pages": physical_count, "failed": sum(p.outcome == "failed" for p in pages),
                    "needs_review": sum(p.outcome == "needs_review" for p in pages),
                    "output_sha256": extraction.output_sha256, "human_verified": False}

    async def fail(self, claim, extraction_id, code: str):
        async with self.sessions() as session, session.begin():
            job, attempt, now = await self._owned(session, claim)
            attempt.status, attempt.completed_at, attempt.error_message = "failed", now, code
            count = (await session.execute(select(func.count()).select_from(JobAttempt).where(
                JobAttempt.job_id == job.id, JobAttempt.started_at >= job.started_at))).scalar_one()
            job.status = "failed" if count >= self.max_attempts else "queued"
            job.completed_at = now if job.status == "failed" else None
            job.error_message, job.lease_until = code, None
            if extraction_id:
                extraction = await session.get(FileExtraction, extraction_id)
                if extraction and extraction.processing_status != "completed":
                    extraction.processing_status = "failed"

    async def retry(self, job_id: UUID):
        async with self.sessions() as session, session.begin():
            job = (await session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update())).scalar_one_or_none()
            if job is None or job.task_type != "extract" or job.target_file_id is None:
                raise JobConflict("extraction_job_not_found")
            if job.status not in {"failed", "completed"}:
                raise JobConflict("retry_requires_failed_or_completed_job")
            # Preserve all pages, blocks and attempts. A new generation produces
            # another extraction. No verified or partial data is deleted.
            job.status, job.started_at, job.completed_at = "queued", None, None
            job.error_message, job.lease_until, job.progress_done = None, None, 0

    async def inspect_job(self, job_id):
        async with self.sessions() as session:
            job = await session.get(ProcessingJob, job_id)
            if job is None:
                raise JobConflict("job_not_found")
            attempts = list((await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)
                                                   .order_by(JobAttempt.attempt_number))).all())
            return {"id": str(job.id), "task_type": job.task_type, "file_id": str(job.target_file_id),
                    "status": job.status, "progress_done": job.progress_done, "progress_total": job.progress_total,
                    "error": job.error_message, "lease_until": job.lease_until.isoformat() if job.lease_until else None,
                    "attempts": [{"number": a.attempt_number, "status": a.status, "error": a.error_message,
                                  "heartbeat_at": a.heartbeat_at.isoformat() if a.heartbeat_at else None} for a in attempts]}
