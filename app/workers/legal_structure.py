"""Process queued draft legal-structure revisions outside HTTP requests.

    python -m app.workers.legal_structure --once
    python -m app.workers.legal_structure --watch
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository
from app.services.legal_structure import parse_structure
from app.workers.runner import run_jobs, run_until_stopped


logger = logging.getLogger(__name__)


async def process_one(repo: LegalStructureRepository, *, lease_seconds: int = 900) -> bool:
    claim = await repo.claim_next(worker_id=f"{socket.gethostname()}:{os.getpid()}",
                                  lease_seconds=lease_seconds)
    if claim is None:
        return False
    try:
        documents = await repo.load_sources(claim.revision_id)
        # Parsing a long statute is CPU work; keep it off the event loop.
        parsed = await asyncio.to_thread(parse_structure, documents)
        await repo.complete(claim, parsed, documents)
        logger.info("Structured revision %s: %d provisions, %d spans, %d warnings",
                    claim.revision_id, len(parsed.nodes), len(parsed.spans), len(parsed.warnings))
    except asyncio.CancelledError:
        await asyncio.shield(repo.fail(claim, "Worker stopped before the job finished; retry the job."))
        raise
    except Exception as exc:
        logger.exception("Structure job %s failed", claim.job_id)
        await repo.fail(claim, str(exc))
    return True


async def _run(*, watch: bool, poll_seconds: float) -> None:
    settings = get_settings()
    db = Database(settings)
    try:
        if not await db.connect():
            raise RuntimeError("Cannot parse legal structure while database is unavailable.")
        repo = LegalStructureRepository(db)
        await run_jobs(lambda: process_one(repo, lease_seconds=settings.legal_worker_lease_seconds),
                       watch=watch, poll_seconds=poll_seconds)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued draft legal-structure jobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run_until_stopped(lambda: _run(watch=args.watch, poll_seconds=max(args.poll_seconds, 0.5)))


if __name__ == "__main__":
    main()
