"""A separate worker; never invoked inside an upload or retry HTTP request."""
import asyncio
from contextlib import suppress

from app.repositories.legal_extraction_repository import JobConflict, LeaseLost
from app.services.legal_pdf_extraction import PdfProblem, extraction_config, failed_page, warning
from app.services.pdf_process import pdf_process


class LegalExtractionWorker:
    def __init__(self, repository, settings, *, processor=pdf_process):
        self.repository, self.settings, self.processor = repository, settings, processor

    async def run_once(self, worker_id, job_id=None):
        config = extraction_config(self.settings)
        claim = await self.repository.claim(worker_id, job_id)
        if claim is None:
            return {"claimed": False, "reason": "No eligible job; inspect its status or lease."}
        lost = asyncio.Event()

        async def renew():
            try:
                while True:
                    await asyncio.sleep(self.settings.legal_worker_lease_seconds / 3)
                    await self.repository.heartbeat(claim)
            except Exception:
                lost.set()

        heartbeat = asyncio.create_task(renew())
        extraction_id = None
        try:
            extraction_id, source = await self.repository.prepare(claim, config)
            source["root"] = self.settings.legal_upload_dir
            try:
                info = await self.processor({"operation": "inspect", "source": source}, self.settings.legal_page_timeout)
            except PdfProblem as exc:
                # Unreadable PDF bytes still get explicit failed outcomes for
                # registered pages. Physical coverage remains unverified.
                if str(exc) in {"pdf_unreadable", "pdf_encrypted_or_empty"}:
                    await self.repository.set_total(claim, source["page_count"], extraction_id)
                    for index in range(1, source["page_count"] + 1):
                        await self.repository.save_page(claim, extraction_id, failed_page(index, str(exc)))
                raise
            count = info["page_count"]
            if count > self.settings.legal_upload_max_pages:
                raise PdfProblem("physical_page_limit_exceeded")
            await self.repository.set_total(claim, count, extraction_id)
            existing = await self.repository.coverage(extraction_id)
            for index in range(1, count + 1):
                if lost.is_set():
                    raise LeaseLost("Worker lease renewal failed")
                if index in existing:
                    continue
                try:
                    page = await self.processor({"operation": "page", "source": source,
                        "config": config, "page_index": index}, self.settings.legal_page_timeout)
                except PdfProblem as exc:
                    if str(exc) in {"source_hash_mismatch", "source_size_mismatch", "source_pdf_missing", "unsafe_pdf_location"}:
                        raise
                    page = failed_page(index, str(exc))
                if count != source["page_count"]:
                    page["warnings"].append(warning("page_count_mismatch", "Physical and registered page counts differ.",
                                                    registered=source["page_count"], physical=count))
                    if page["outcome"] == "extracted":
                        page["outcome"] = "needs_review"
                await self.repository.save_page(claim, extraction_id, page)
            if lost.is_set():
                raise LeaseLost("Worker lease renewal failed")
            return {"claimed": True, **await self.repository.finish(claim, extraction_id, count)}
        except LeaseLost:
            return {"claimed": True, "status": "lease_lost", "job_id": str(claim.job_id)}
        except Exception as exc:
            code = str(exc) if isinstance(exc, (PdfProblem, JobConflict)) else "worker_processing_failed"
            with suppress(LeaseLost):
                await self.repository.fail(claim, extraction_id, code)
            return {"claimed": True, "status": "failed", "error": code, "job_id": str(claim.job_id)}
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
