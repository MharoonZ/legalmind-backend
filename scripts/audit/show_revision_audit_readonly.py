"""Print persisted read-only structural/source findings for selected revisions."""

import argparse
import asyncio
import json
from uuid import UUID

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository


async def main(revision_ids: list[UUID]) -> None:
    db = Database(get_settings())
    if not await db.connect():
        raise RuntimeError("Database unavailable")
    try:
        repo = LegalStructureRepository(db)
        for revision_id in revision_ids:
            audit = await repo.audit(revision_id)
            print(json.dumps({"revision": str(revision_id), "audit": audit}, default=str),
                  flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="+", type=UUID)
    args = parser.parse_args()
    asyncio.run(main(args.revision))
