"""Inspect or explicitly approve the latest saved revision of each legal edition.

Default is read-only. --apply records local Phase B indexing decisions with
unresolved audit/page findings preserved in immutable review events.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.legal_corpus_models import CorpusRevision, LegalInstrument, LegalVersion
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository, StructureConflict


async def run(*, apply: bool, reason: str | None) -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Development database is unavailable.")
        latest = (select(CorpusRevision.legal_version_id,
                         func.max(CorpusRevision.revision_number).label("number"))
                  .group_by(CorpusRevision.legal_version_id).subquery())
        async with db.session() as session:
            rows = (await session.execute(
                select(CorpusRevision.id, LegalInstrument.canonical_title)
                .join(latest, (latest.c.legal_version_id == CorpusRevision.legal_version_id)
                      & (latest.c.number == CorpusRevision.revision_number))
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .order_by(LegalInstrument.canonical_title)
            )).all()
        repo = LegalStructureRepository(db)
        totals = {"approved": 0, "already_approved": 0, "blocked": 0, "ready": 0}
        semaphore = asyncio.Semaphore(4)

        async def process(revision_id, title):
            async with semaphore:
                return await inspect_and_maybe_approve(revision_id, title)

        async def inspect_and_maybe_approve(revision_id, title):
            check = await repo.approval_check(revision_id)
            row = {"title": title, "revision_id": str(revision_id),
                   "state": check["review_state"],
                   "hard_blockers": check["hard_blockers"],
                   "audit_errors": check["error_count"],
                   "audit_warnings": check["warning_count"],
                   "pages_needing_review": len(check["page_review"])}
            if check["review_state"] == "approved":
                row["result"] = "already_approved"
            elif check["hard_blockers"]:
                row["result"] = "blocked"
            elif not apply:
                row["result"] = "eligible_for_explicit_decision"
            else:
                try:
                    result = await repo.approve(
                        revision_id, expected_content_sha256=check["content_sha256"],
                        reason=reason, acknowledge_findings=True,
                    )
                    row["result"] = result["review_state"]
                except StructureConflict as exc:
                    row["result"] = "blocked"
                    row["hard_blockers"] = [str(exc)]
            return row

        for row in await asyncio.gather(*(process(revision_id, title)
                                          for revision_id, title in rows)):
            key = "ready" if row["result"] == "eligible_for_explicit_decision" else row["result"]
            totals[key] += 1
            print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        print(json.dumps({"total_editions": len(rows), **totals}), flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Record local Phase B approval decisions for non-blocked latest drafts")
    parser.add_argument("--reason", help="Reviewer decision reason (required with --apply)")
    args = parser.parse_args()
    if args.apply and (not args.reason or len(args.reason.strip()) < 10):
        parser.error("--apply requires --reason with at least 10 characters")
    asyncio.run(run(apply=args.apply, reason=args.reason))
