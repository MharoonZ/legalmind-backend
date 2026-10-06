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


logger = logging.getLogger(__name__)


async def process_one(repo: LegalStructureRepository) -> bool:
    claim = await repo.claim_next(worker_id=f"{socket.gethostname()}:{os.getpid()}")
    if claim is None:
        return False
    try:
        documents = await repo.load_sources(claim.revision_id)
        parsed = parse_structure(documents)
        await repo.complete(claim, parsed, documents)
        logger.info("Structured revision %s: %d provisions, %d spans, %d warnings",
                    claim.revision_id, len(parsed.nodes), len(parsed.spans), len(parsed.warnings))
    except Exception as exc:
        logger.exception("Structure job %s failed", claim.job_id)
        await repo.fail(claim, str(exc))
    return True


async def _run(*, watch: bool, poll_seconds: float) -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Cannot parse legal structure while database is unavailable.")
        repo = LegalStructureRepository(db)
        while True:
            worked = await process_one(repo)
            if not watch:
                return
            if not worked:
                await asyncio.sleep(poll_seconds)
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
    asyncio.run(_run(watch=args.watch, poll_seconds=max(args.poll_seconds, 0.5)))


if __name__ == "__main__":
    main()
