"""Durable legal-PDF worker. Requires an explicit job ID or --all-queued.

Examples from backend root:
  python -m scripts.run_legal_extraction --job-id UUID --inspect
  python -m scripts.run_legal_extraction --job-id UUID
  python -m scripts.run_legal_extraction --job-id UUID --retry
  python -m scripts.run_legal_extraction --ocr-check
"""
import argparse
import asyncio
import json
import socket
from uuid import UUID, uuid4

from app.core.config import Settings
from app.db.session import Database
from app.repositories.legacy_legal_extraction_repository import LegalExtractionRepository
from app.services.legal_extraction_worker import LegalExtractionWorker
from app.services.legal_pdf_extraction import extraction_config, resolve_pdf


async def main(args):
    settings = Settings()
    if args.ocr_check:
        print(json.dumps(extraction_config(settings)["ocr"], indent=2))
        return 0
    if args.job_id is None and not args.all_queued:
        raise ValueError("Supply --job-id or explicitly opt in to --all-queued")
    if (args.retry or args.inspect) and not args.job_id:
        raise ValueError("--retry and --inspect require --job-id")
    if args.retry and args.inspect:
        raise ValueError("Inspect first; run --retry separately")
    if not settings.legal_ingest_enabled or settings.environment.lower() != "development":
        raise ValueError("Worker requires LEGAL_INGEST_ENABLED=true and ENVIRONMENT=development")
    database = Database(settings)
    repository = LegalExtractionRepository(database.session, lease_seconds=settings.legal_worker_lease_seconds,
                                            max_attempts=settings.legal_worker_max_attempts)
    try:
        if args.inspect:
            report = await repository.inspect_job(args.job_id)
            from app.db.legal_corpus_models import LegalFile
            async with database.session() as session:
                source = await session.get(LegalFile, UUID(report["file_id"]))
                report["registered_pages"] = source.page_count
                report["object_key"] = source.object_key
                report["sha256"] = source.sha256
                try:
                    path = resolve_pdf(settings.legal_upload_dir, source.object_key, source.sha256)
                    report["source_exists"] = path.is_file()
                except Exception:
                    report["source_exists"] = False
            print(json.dumps(report, indent=2))
            return 0
        if args.retry:
            await repository.retry(args.job_id)
        worker = LegalExtractionWorker(repository, settings)
        worker_id = f"{socket.gethostname()}:{uuid4()}"
        while True:
            result = await worker.run_once(worker_id, args.job_id)
            print(json.dumps(result, indent=2), flush=True)
            if not args.watch:
                return 2 if result.get("status") in {"failed", "lease_lost"} else 0
            await asyncio.sleep(5)
    finally:
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--job-id", type=UUID)
    selector.add_argument("--all-queued", action="store_true")
    parser.add_argument("--inspect", action="store_true", help="Read-only status and original-file availability")
    parser.add_argument("--retry", action="store_true", help="Explicitly start a new generation; retain all old rows")
    parser.add_argument("--watch", action="store_true", help="Poll for eligible work, still respecting --job-id")
    parser.add_argument("--ocr-check", action="store_true")
    try:
        raise SystemExit(asyncio.run(main(parser.parse_args())))
    except (Exception,) as exc:
        # Never dump settings, credentials or SQL parameters to the console.
        print(json.dumps({"error": type(exc).__name__, "message": "Worker command failed. Check local configuration and database access."}))
        raise SystemExit(2)
