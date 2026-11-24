"""Run queued legal-PDF page extraction outside the upload HTTP request.

    python -m app.workers.legal_extract --once
    python -m app.workers.legal_extract --watch
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import socket
from pathlib import Path

import fitz

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_extraction_repository import LegalExtractionRepository
from app.services.legal_extraction import (
    ExtractedPage,
    ExtractionConfig,
    apply_reviewed_edits,
    extract_page,
    resolve_source_pdf,
    verify_source_pdf,
    warning,
)

from app.workers.runner import run_jobs, run_until_stopped

logger = logging.getLogger(__name__)


async def process_one(repo: LegalExtractionRepository, *, upload_dir: str, config: ExtractionConfig,
                      lease_seconds: int = 900) -> bool:
    """Return False when there was no queued/expired job to claim."""
    manifest = config.review_manifest or {}
    target_hash = manifest.get("source_sha256") if manifest else None
    if manifest and (not isinstance(target_hash, str)
                     or re.fullmatch(r"[0-9a-f]{64}", target_hash) is None):
        raise ValueError("A review manifest must identify one registered PDF SHA-256.")
    claim = await repo.claim_next(
        worker_id=f"{socket.gethostname()}:{os.getpid()}", config=config,
        expected_sha256=target_hash, lease_seconds=lease_seconds,
    )
    if claim is None:
        return False
    logger.info("Extracting file %s (%d pages), job %s", claim.file_id, claim.page_count, claim.job_id)
    digest = hashlib.sha256()
    try:
        path = resolve_source_pdf(upload_dir, claim.object_key, claim.sha256)
        verify_source_pdf(path, claim.sha256)
        if manifest and manifest.get("source_sha256") != claim.sha256:
            raise ValueError("The reviewed extraction manifest targets a different PDF hash.")
        with fitz.open(path) as document:
            if document.page_count != claim.page_count:
                raise ValueError("The registered page count differs from the stored PDF.")
            edits_by_page = manifest.get("pages", {})
            if any(not str(index).isdigit() or not 1 <= int(index) <= claim.page_count
                   for index in edits_by_page):
                raise ValueError("Reviewed extraction manifest names a page outside the PDF.")
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
                edits = edits_by_page.get(str(index + 1))
                if edits is not None:
                    result = apply_reviewed_edits(
                        result, edits, reviewer=manifest.get("reviewer", ""),
                    )
                await repo.save_page(claim, result, lease_seconds=lease_seconds)
                page_digest = result.digest_payload()
                digest.update(len(page_digest).to_bytes(8, "big"))
                digest.update(page_digest)
                logger.info(
                    "File %s page %d/%d: %s (%s)", claim.file_id,
                    index + 1, claim.page_count, result.outcome, result.extraction_method,
                )
        await repo.complete(claim, digest.hexdigest())
        logger.info("Completed extraction %s", claim.extraction_id)
    except asyncio.CancelledError:
        # Stopped mid-job: release it now so it can be retried at once,
        # rather than staying "running" until the lease runs out.
        await asyncio.shield(repo.fail(claim, "Worker stopped before the job finished; retry the job."))
        raise
    except Exception as exc:
        logger.exception("Extraction job %s failed", claim.job_id)
        await repo.fail(claim, str(exc))
    return True


async def _run(*, watch: bool, poll_seconds: float, review_manifest_path: str | None) -> None:
    settings = get_settings()
    db = Database(settings)
    try:
        if not await db.connect():
            raise RuntimeError("Cannot run extraction while the database is unavailable.")
        repo = LegalExtractionRepository(db)
        manifest = json.loads(Path(review_manifest_path).read_text(encoding="utf-8")) if review_manifest_path else None
        config = ExtractionConfig(
            tessdata_dir=settings.legal_ocr_tessdata_dir,
            languages=settings.legal_ocr_languages,
            dpi=settings.legal_ocr_dpi,
            review_manifest=manifest,
        )
        await run_jobs(
            lambda: process_one(repo, upload_dir=settings.legal_upload_dir, config=config,
                                lease_seconds=settings.legal_worker_lease_seconds),
            watch=watch, poll_seconds=poll_seconds)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued legal-PDF extraction jobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process at most one job (default)")
    mode.add_argument("--watch", action="store_true", help="Keep polling for queued jobs")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--review-manifest", help="JSON source-hash-bound visual corrections for one PDF")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run_until_stopped(lambda: _run(watch=args.watch, poll_seconds=max(args.poll_seconds, 0.5),
                                   review_manifest_path=args.review_manifest))


if __name__ == "__main__":
    main()
