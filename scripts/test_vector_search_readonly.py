"""Test vector search against the embedded legal corpus (read-only).

Internal development diagnostic: may inspect approved unpublished chunks.
For publication-scoped Stage 10 search use ``python -m scripts.search_legal``.

Encodes a query string with the active profile and returns the top-k most
similar chunks from all approved revisions.

Usage:
    .venv\\Scripts\\python -m scripts.test_vector_search_readonly "consumer protection rights"
    .venv\\Scripts\\python -m scripts.test_vector_search_readonly "punishment for gambling" --top-k 5
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.db.session import Database
from app.embeddings.encoder import encode_texts
from app.embeddings.profiles import ACTIVE_PROFILE
from app.embeddings.repository import EmbeddingRepository


async def run(query: str, top_k: int) -> None:
    profile = ACTIVE_PROFILE
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable.")

        repo = EmbeddingRepository(db)

        # Resolve profile_id
        profile_id = await repo.get_profile_id(profile)
        if profile_id is None:
            raise RuntimeError("The active embedding profile has not been built; this read-only command will not create it.")

        # Embed the query (single text, batch_size=1)
        vectors = encode_texts([query], profile, batch_size=1)
        query_vector = vectors[0]

        # Search
        results = await repo.vector_search(
            query_vector, profile_id, limit=top_k
        )

        print(json.dumps({
            "query": query,
            "profile": profile.name,
            "results": [
                {
                    "rank": i + 1,
                    "vector_score": round(r["vector_score"], 4),
                    "instrument_title": r["instrument_title"],
                    "reference_path": r["reference_path"],
                    "heading": r["heading"],
                    "chunk_text_preview": r["chunk_text"][:200] + "…" if len(r["chunk_text"]) > 200 else r["chunk_text"],
                }
                for i, r in enumerate(results)
            ],
        }, indent=2, ensure_ascii=False))
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Query string to search for.")
    parser.add_argument("--top-k", type=int, default=10, help="Number of results (default: 10).")
    args = parser.parse_args()
    asyncio.run(run(args.query, args.top_k))

