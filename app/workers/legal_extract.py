"""Run queued legal-PDF page extraction outside the upload HTTP request.

    python -m app.workers.legal_extract --once
    python -m app.workers.legal_extract --watch
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import socket

import fitz

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_extraction_repository import LegalExtractionRepository
from app.services.legal_extraction import (
    ExtractedPage,
    ExtractionConfig,
    extract_page,
    resolve_source_pdf,
    verify_source_pdf,
    warning,
)

logger = logging.getLogger(__name__)


async def process_one(repo: LegalExtractionRepository, *, upload_dir: str, config: ExtractionConfig) -> bool:
    """Return False when there was no queued/expired job to claim."""
    claim = await repo.claim_next(
        worker_id=f"{socket.gethostname()}:{os.getpid()}", config=config
    )
    if claim is None:
        return False
    logger.info("Extracting file %s (%d pages), job %s", claim.file_id, claim.page_count, claim.job_id)
    digest = hashlib.sha256()
    try:
        path = resolve_source_pdf(upload_dir, claim.object_key, claim.sha256)
        verify_source_pdf(path, claim.sha256)
        with fitz.open(path) as document:
            if document.page_count != claim.page_count:
                raise ValueError("The registered page count differs from the stored PDF.")
            for index in range(claim.page_count):
                try:
                    page = document.load_page(index)
                    result = extract_page(page, index + 1, config)
                except Exception as exc:  # Account for the page and continue.
                    result = ExtractedPage(
                        page_index=index + 1, width_pt=None, height_pt=None,
                        rotation_deg=None, raw_text="", normalized_text="",
                        extraction_method="none", outcome="failed",
                        warnings=[warning("page_failed", f"Page extraction failed: {exc}", "error")],
                        blocks=[],
                    )
                await repo.save_page(claim, result)
                page_digest = result.digest_payload()
                digest.update(len(page_digest).to_bytes(8, "big"))
                digest.update(page_digest)
                logger.info(
                    "File %s page %d/%d: %s (%s)", claim.file_id,
                    index + 1, claim.page_count, result.outcome, result.extraction_method,
                )
        await repo.complete(claim, digest.hexdigest())
        logger.info("Completed extraction %s", claim.extraction_id)
    except Exception as exc:
        logger.exception("Extraction job %s failed", claim.job_id)
        await repo.fail(claim, str(exc))
    return True


async def _run(*, watch: bool, poll_seconds: float) -> None:
    settings = get_settings()
    db = Database(settings)
    try:
        if not await db.connect():
            raise RuntimeError("Cannot run extraction while the database is unavailable.")
        repo = LegalExtractionRepository(db)
        config = ExtractionConfig(
            tessdata_dir=settings.legal_ocr_tessdata_dir,
            languages=settings.legal_ocr_languages,
            dpi=settings.legal_ocr_dpi,
        )
        while True:
            worked = await process_one(repo, upload_dir=settings.legal_upload_dir, config=config)
            if not watch:
                return
            if not worked:
                await asyncio.sleep(poll_seconds)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued legal-PDF extraction jobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process at most one job (default)")
    mode.add_argument("--watch", action="store_true", help="Keep polling for queued jobs")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_run(watch=args.watch, poll_seconds=max(args.poll_seconds, 0.5)))


if __name__ == "__main__":
    main()
