"""Build legal-aware chunk sets for approved corpus revisions.

Chunking is a derived, idempotent, re-runnable step over data Haroon's
pipeline already extracted and locally approved for Phase B (see
docs/ABDULLAH_PHASE_B_HANDOVER.md). Unlike structure approval, it never
changes legal review state, so it runs by default; pass --dry-run to only
report what would happen without writing anything.

By default this processes every revision with review_state='approved'. Pass
--revision-id to chunk one specific revision instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import CorpusRevision, LegalInstrument, LegalVersion
from app.db.session import Database
from app.repositories.legal_chunk_repository import ChunkingError, LegalChunkRepository
from app.services.legal_chunking import ChunkConfig


async def run(*, dry_run: bool, revision_id: UUID | None, max_chars: int) -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Development database is unavailable.")
        async with db.session() as session:
            query = (select(CorpusRevision.id, LegalInstrument.canonical_title)
                     .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                     .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                     .order_by(LegalInstrument.canonical_title))
            if revision_id is not None:
                query = query.where(CorpusRevision.id == revision_id)
            else:
                query = query.where(CorpusRevision.review_state == "approved")
            rows = (await session.execute(query)).all()
        if not rows:
            print(json.dumps({"total_revisions": 0, "message": "No matching revisions found."}))
            return

        repo = LegalChunkRepository(db)
        config = ChunkConfig(max_chars=max_chars)
        totals = {"created": 0, "already_built": 0, "blocked": 0}
        for found_id, title in rows:
            row = {"title": title, "revision_id": str(found_id)}
            if dry_run:
                existing = await repo.latest_chunk_set(found_id)
                row["result"] = "dry_run"
                row["existing_chunk_set_id"] = str(existing["chunk_set_id"]) if existing else None
                print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
                continue
            try:
                outcome = await repo.build_for_revision(found_id, config=config)
                row["chunk_set_id"] = str(outcome["chunk_set_id"])
                row["chunk_count"] = outcome["chunk_count"]
                row["result"] = "created" if outcome["created"] else "already_built"
                totals[row["result"]] += 1
            except ChunkingError as exc:
                row["result"] = "blocked"
                row["reason"] = str(exc)
                totals["blocked"] += 1
            print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        if not dry_run:
            print(json.dumps({"total_revisions": len(rows), **totals}), flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report existing chunk sets; build nothing.")
    parser.add_argument("--revision-id", type=UUID,
                        help="Chunk one specific revision instead of every approved revision.")
    parser.add_argument("--max-chars", type=int, default=ChunkConfig().max_chars,
                        help="Soft size budget per chunk, in characters (default: %(default)s).")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, revision_id=args.revision_id, max_chars=args.max_chars))
