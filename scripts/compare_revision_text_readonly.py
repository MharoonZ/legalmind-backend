"""Compare saved provision text ownership with a fresh in-memory parse.

Pass one or more persisted revision IDs. Never writes a revision or review event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import Provision
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository
from app.services.legal_structure import parse_structure


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def difference_context(left: str, right: str) -> dict:
    first = next((index for index, (a, b) in enumerate(zip(left, right)) if a != b),
                 min(len(left), len(right)))
    return {
        "first_char": first,
        "saved": left[max(0, first - 70):first + 180],
        "preview": right[max(0, first - 70):first + 180],
    }


async def main(revision_ids: list[UUID]) -> None:
    db = Database(get_settings())
    if not await db.connect():
        raise RuntimeError("Database unavailable")
    try:
        repo = LegalStructureRepository(db)
        for revision_id in revision_ids:
            async with db.session() as session:
                saved = (await session.scalars(select(Provision).where(
                    Provision.revision_id == revision_id))).all()
            source = await repo.load_sources(revision_id)
            if not source:
                raise RuntimeError(f"No sources for {revision_id}")
            preview = parse_structure(source)
            old = {node.reference_path: node for node in saved}
            new = {node.reference_path: node for node in preview.nodes}
            substantive = []
            whitespace_only = []
            for path in sorted(old.keys() & new.keys()):
                if old[path].own_text == new[path].own_text:
                    continue
                if compact(old[path].own_text) == compact(new[path].own_text):
                    whitespace_only.append(path)
                else:
                    substantive.append({
                        "path": path, "kind": old[path].kind,
                        "saved_length": len(old[path].own_text),
                        "preview_length": len(new[path].own_text),
                        "difference": difference_context(old[path].own_text,
                                                         new[path].own_text),
                    })
            print(json.dumps({
                "revision": str(revision_id),
                "title": source[0].title,
                "added_paths": sorted(new.keys() - old.keys()),
                "removed_paths": sorted(old.keys() - new.keys()),
                "whitespace_only_paths": whitespace_only,
                "substantive_changes": substantive,
            }, ensure_ascii=False, default=str), flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="+", type=UUID)
    args = parser.parse_args()
    asyncio.run(main(args.revision))
