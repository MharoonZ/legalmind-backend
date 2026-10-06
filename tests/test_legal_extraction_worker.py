"""Worker recovery using real local persistence and synthetic PDFs only."""
import asyncio
from datetime import timedelta

import fitz
import pytest
from sqlalchemy.orm import Session

from app.db.legal_corpus_models import LegalFile, ProcessingJob
from app.services.legal_extraction_worker import LegalExtractionWorker
from app.services.legal_pdf_extraction import PdfProblem, execute
from tests.test_legal_extraction_repository import repository, seed  # noqa: F401 - fixture
from tests.test_legal_pdf_extraction import store_pdf


def setup_job(repository, settings, tmp_path, *, registered=6, physical=6, corrupt=False):
    with fitz.open() as doc:
        for index in range(physical):
            page = doc.new_page()
            if index != 3:
                page.insert_text((35, 60), f"Synthetic page {index + 1}: enough native words for an ordinary extraction fixture.")
        source = store_pdf(tmp_path, b"broken PDF" if corrupt else doc.tobytes())
    job_id = seed(repository, pages=registered)
    with Session(repository.engine) as session, session.begin():
        job = session.get(ProcessingJob, job_id)
        file = session.get(LegalFile, job.target_file_id)
        for key in ("object_key", "byte_size", "sha256"): setattr(file, key, source[key])
    configured = settings.model_copy(update={"legal_upload_dir": str(tmp_path), "legal_tesseract_path": "missing-test.exe"})
    return job_id, configured


@pytest.mark.anyio
async def test_six_physical_pages_include_textless_failure(repository, settings, tmp_path):
    job_id, settings = setup_job(repository, settings, tmp_path)
    async def process(payload, timeout): return execute(payload)
    report = await LegalExtractionWorker(repository, settings, processor=process).run_once("test", job_id)
    assert report["processing_status"] == "completed"
    assert report["pages"] == 6 and report["failed"] == 1
    assert report["human_verified"] is False
    assert (await repository.inspect_job(job_id))["progress_done"] == 6


@pytest.mark.anyio
async def test_interruption_resumes_only_unrecorded_pages(repository, settings, tmp_path):
    job_id, settings = setup_job(repository, settings, tmp_path)
    calls = []
    async def interrupted(payload, timeout):
        if payload["operation"] == "page" and payload["page_index"] == 2: raise asyncio.CancelledError()
        return execute(payload)
    with pytest.raises(asyncio.CancelledError):
        await LegalExtractionWorker(repository, settings, processor=interrupted).run_once("old", job_id)
    assert (await repository.inspect_job(job_id))["progress_done"] == 1
    repository.now += timedelta(seconds=31)
    async def resumed(payload, timeout):
        if payload["operation"] == "page": calls.append(payload["page_index"])
        return execute(payload)
    report = await LegalExtractionWorker(repository, settings, processor=resumed).run_once("new", job_id)
    assert report["processing_status"] == "completed"
    assert calls == [2, 3, 4, 5, 6]


@pytest.mark.anyio
async def test_corrupt_pdf_records_failures_without_false_completion(repository, settings, tmp_path):
    job_id, settings = setup_job(repository, settings, tmp_path, corrupt=True)
    async def process(payload, timeout): return execute(payload)
    result = await LegalExtractionWorker(repository, settings, processor=process).run_once("test", job_id)
    assert result["status"] == "failed" and result["error"] == "pdf_unreadable"
    job = await repository.inspect_job(job_id)
    assert job["progress_done"] == 6 and job["status"] == "queued"  # retry budget remains


@pytest.mark.anyio
async def test_count_discrepancy_and_page_timeout_remain_visible(repository, settings, tmp_path):
    job_id, settings = setup_job(repository, settings, tmp_path, registered=5)
    async def process(payload, timeout):
        if payload["operation"] == "page" and payload["page_index"] == 2: raise PdfProblem("page_timeout")
        return execute(payload)
    result = await LegalExtractionWorker(repository, settings, processor=process).run_once("test", job_id)
    assert result["pages"] == 6 and result["failed"] == 2 and result["needs_review"] == 4


@pytest.mark.anyio
async def test_file_integrity_failure_does_not_claim_success(repository, settings, tmp_path):
    job_id, settings = setup_job(repository, settings, tmp_path)
    async def process(payload, timeout): raise PdfProblem("source_hash_mismatch")
    result = await LegalExtractionWorker(repository, settings, processor=process).run_once("test", job_id)
    assert result["error"] == "source_hash_mismatch"
    assert (await repository.inspect_job(job_id))["progress_done"] == 0


@pytest.mark.anyio
async def test_heartbeat_renews_during_slow_page_processing(repository, settings, tmp_path):
    job_id, settings = setup_job(repository, settings, tmp_path, registered=1, physical=1)
    settings = settings.model_copy(update={"legal_worker_lease_seconds": 0.03})
    beats = []
    original = repository.heartbeat
    async def heartbeat(claim):
        beats.append(claim.attempt_id)
        await original(claim)
    repository.heartbeat = heartbeat
    async def process(payload, timeout):
        await asyncio.sleep(0.06)
        return execute(payload)
    result = await LegalExtractionWorker(repository, settings, processor=process).run_once("test", job_id)
    assert result["processing_status"] == "completed" and len(beats) >= 2
