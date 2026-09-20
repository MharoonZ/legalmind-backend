"""Record a human review decision for one corpus version.

    python scripts/approve_source.py --list
    python scripts/approve_source.py --source-id ca1872:<hash> --reviewer "Haroon Zahid"
    python scripts/approve_source.py --source-id ca1872:<hash> --reject --reviewer "..."

Why this exists: the importer always writes `needs_review`, and the schema
refuses to mark a source approved without a reviewer name and a timestamp.
Approval is a person's decision, so it gets its own deliberate command instead
of happening quietly inside an import.

Until a source is approved, keep RETRIEVAL_REVIEW_STATUSES=needs_review,approved
for development, and set it to `approved` before any real user sees an answer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import Database  # noqa: E402


async def main(args: argparse.Namespace) -> None:
    db = Database(get_settings())
    try:
        async with db.session() as session:
            if args.list or not args.source_id:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, law_name, review_status, reviewed_by, reviewed_at "
                            "FROM law_sources ORDER BY created_at"
                        )
                    )
                ).all()
                if not rows:
                    print("No sources imported yet.")
                for row in rows:
                    reviewer = row.reviewed_by or "-"
                    print(f"{row.review_status:<13} {reviewer:<20} {row.id}  ({row.law_name})")
                return

            status = "rejected" if args.reject else "approved"
            result = await session.execute(
                text(
                    "UPDATE law_sources SET review_status = :status, "
                    "reviewed_by = :reviewer, reviewed_at = :reviewed_at "
                    "WHERE id = :source_id"
                ),
                {
                    "status": status,
                    "reviewer": args.reviewer,
                    "reviewed_at": datetime.now(timezone.utc),
                    "source_id": args.source_id,
                },
            )
            if result.rowcount == 0:
                print(f"No source with id {args.source_id}")
                return
            await session.commit()
            print(f"{args.source_id} -> {status} by {args.reviewer}")
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show every source and its status")
    parser.add_argument("--source-id", help="the law_sources.id to update")
    parser.add_argument("--reviewer", default="", help="name of the person who reviewed it")
    parser.add_argument("--reject", action="store_true", help="mark rejected instead of approved")
    parsed = parser.parse_args()
    if parsed.source_id and not parsed.reviewer:
        parser.error("--reviewer is required when changing a review status")
    asyncio.run(main(parsed))
