"""Read-only manual check of legal_chunks keyword search against a chunk set.

Exercises both lookup paths added for Step 3 (Build keyword search):

* exact label lookup (``LegalChunkRepository.find_by_label``) — proves a
  precise statutory reference like "19A" never also returns "19";
* lexical/full-text search (``LegalChunkRepository.keyword_search``) over
  the GIN-indexed ``search_vector`` column.

Nothing is written; this only SELECTs through the repository. Usage:

    python scripts/test_keyword_search_readonly.py <chunk-set-id> \\
        --label 19 --label 19A --query "false statements"

If no chunk set id is given, the script picks the most recently built one
automatically (requires at least one revision already chunked via
scripts/build_legal_chunks.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import ChunkSet
from app.db.session import Database
from app.repositories.legal_chunk_repository import LegalChunkRepository


async def _latest_chunk_set_id(db: Database) -> UUID | None:
    async with db.session() as session:
        row = (await session.execute(
            select(ChunkSet.id).order_by(ChunkSet.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        return row


async def main(chunk_set_id: UUID | None, labels: list[str], queries: list[str]) -> None:
    db = Database(get_settings())
    if not await db.connect():
        raise RuntimeError("Development database is unavailable.")
    try:
        if chunk_set_id is None:
            chunk_set_id = await _latest_chunk_set_id(db)
            if chunk_set_id is None:
                print(json.dumps({"error": "No chunk sets exist yet; run "
                                            "scripts/build_legal_chunks.py first."}))
                return
            print(json.dumps({"using_latest_chunk_set_id": str(chunk_set_id)}), flush=True)

        repo = LegalChunkRepository(db)

        for label in labels:
            matches = await repo.find_by_label(chunk_set_id, label)
            print(json.dumps({
                "exact_label_lookup": label,
                "match_count": len(matches),
                "matches": [{
                    "chunk_id": str(m["id"]), "label": m["label"],
                    "reference_path": m["reference_path"], "prefix": m["prefix"],
                } for m in matches],
            }, ensure_ascii=False, default=str), flush=True)

        if len(labels) >= 2:
            seen_labels = {m["label"] for label in labels
                          for m in await repo.find_by_label(chunk_set_id, label)}
            print(json.dumps({
                "distinctness_check": {
                    "requested_labels": labels,
                    "labels_actually_returned": sorted(seen_labels),
                    "every_requested_label_returned_only_itself":
                        seen_labels <= set(labels),
                }
            }), flush=True)

        for query in queries:
            results = await repo.keyword_search(chunk_set_id, query, limit=5)
            print(json.dumps({
                "keyword_search": query,
                "result_count": len(results),
                "top_results": [{
                    "chunk_id": str(r["id"]), "label": r["label"],
                    "reference_path": r["reference_path"], "rank": r["rank"],
                } for r in results],
            }, ensure_ascii=False, default=str), flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chunk_set_id", type=UUID, nargs="?", default=None,
                        help="Chunk set to query; defaults to the most recently built one.")
    parser.add_argument("--label", action="append", default=[],
                        help="Exact label to look up (repeatable), e.g. --label 19 --label 19A")
    parser.add_argument("--query", action="append", default=[],
                        help="Free-text keyword query to run (repeatable).")
    args = parser.parse_args()
    labels = args.label or ["19", "19A"]
    queries = args.query or ["false statements"]
    asyncio.run(main(args.chunk_set_id, labels, queries))
