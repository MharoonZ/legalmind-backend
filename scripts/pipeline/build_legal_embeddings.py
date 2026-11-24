"""Build dense embeddings for all approved legal chunk sets.

Uses the active embedding profile (BAAI/bge-m3, 1024 dims) to encode every
v2 chunk whose vector is missing, stale, or invalid. Re-running the script
preserves healthy existing vectors and their timestamps.

Prerequisites (run once before this script):
    1. alembic upgrade head          (creates embedding_profiles + legal_chunk_embeddings)
    2. build_legal_chunks.py         (populates legal_chunks for approved revisions)

Typical usage:
    # Encode all approved revisions (recommended first run)
    python -m scripts.pipeline.build_legal_embeddings

    # Encode one revision only
    python -m scripts.pipeline.build_legal_embeddings --revision-id <UUID>

    # Dry-run: show counts without encoding
    python -m scripts.pipeline.build_legal_embeddings --dry-run

    # Use a larger encode batch (faster on GPU)
    python -m scripts.pipeline.build_legal_embeddings --encode-batch-size 32

Progress is printed as JSON lines, one per encode batch, so you can tail -f
or pipe to jq.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from uuid import UUID

from app.core.config import get_settings
from app.db.session import Database
from app.embeddings.encoder import (
    build_embedding_input,
    encode_texts,
    input_sha256,
)
from app.embeddings.profiles import ACTIVE_PROFILE
from app.embeddings.repository import EmbeddingRepository

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("build_legal_embeddings")


async def run(
    *,
    dry_run: bool,
    revision_id: UUID | None,
    encode_batch_size: int,
    fetch_batch_size: int,
) -> None:
    profile = ACTIVE_PROFILE
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Development database is unavailable.")

        repo = EmbeddingRepository(db)

        # 1. Ensure the profile row exists in the DB
        profile_id = await repo.get_profile_id(profile) if dry_run else await repo.ensure_profile(profile)
        if profile_id is None:
            print(json.dumps({"event": "dry_run_done", "profile_exists": False,
                              "message": "No profile exists; run without --dry-run to build."}))
            return
        logger.info(
            "Using profile '%s' (id=%s, model=%s, dim=%d).",
            profile.name, profile_id, profile.model_id, profile.dimension,
        )

        # 2. Show initial stats
        stats = await repo.embedding_stats(profile_id, revision_id=revision_id)
        print(json.dumps({"event": "start", "profile": profile.name, **stats}), flush=True)

        if dry_run:
            print(json.dumps({"event": "dry_run_done", **stats}), flush=True)
            return

        if stats["remaining_chunks"] == 0:
            print(json.dumps({"event": "nothing_to_do", **stats}), flush=True)
            return

        # 3. Encode in batches
        total_upserted = 0
        batch_num = 0
        while True:
            # Fetch a batch of chunks that still need embeddings
            chunks = await repo.chunks_without_embeddings(
                profile_id,
                revision_id=revision_id,
                batch_size=fetch_batch_size,
            )
            if not chunks:
                break
            batch_num += 1

            # Build input texts for this batch
            texts = [
                build_embedding_input(
                    chunk["canonical_title"],
                    chunk["label"],
                    chunk["heading"],
                    chunk["chunk_text"],
                    profile,
                )
                for chunk in chunks
            ]

            # Compute input hashes (for idempotency tracking)
            hashes = [input_sha256(t) for t in texts]

            # Encode (CPU/GPU — this is the slow part)
            t0 = time.perf_counter()
            vectors = encode_texts(texts, profile, batch_size=encode_batch_size)
            elapsed = time.perf_counter() - t0

            # Build rows to upsert
            rows = [
                {
                    "chunk_id": chunk["chunk_id"],
                    "revision_id": chunk["revision_id"],
                    "input_sha256": sha,
                    "embedding": vec,
                }
                for chunk, sha, vec in zip(chunks, hashes, vectors, strict=True)
            ]

            upserted = await repo.upsert_embeddings(profile_id, rows)
            total_upserted += upserted

            print(
                json.dumps({
                    "event": "batch_done",
                    "batch": batch_num,
                    "encoded": len(chunks),
                    "upserted": upserted,
                    "elapsed_s": round(elapsed, 2),
                    "chunks_per_s": round(len(chunks) / elapsed, 1),
                }),
                flush=True,
            )

        # 4. Final stats
        final_stats = await repo.embedding_stats(profile_id, revision_id=revision_id)
        print(
            json.dumps({
                "event": "complete",
                "total_upserted": total_upserted,
                **final_stats,
            }),
            flush=True,
        )
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show counts only; do not load the model or encode anything.",
    )
    parser.add_argument(
        "--revision-id", type=UUID, default=None,
        help="Embed chunks from one specific revision instead of all approved revisions.",
    )
    parser.add_argument(
        "--encode-batch-size", type=int, default=16,
        help="Chunks per model forward pass (default: 16, increase to 32+ on GPU).",
    )
    parser.add_argument(
        "--fetch-batch-size", type=int, default=100,
        help="Chunks fetched from DB per iteration (default: 100).",
    )
    args = parser.parse_args()
    if args.encode_batch_size < 1 or args.fetch_batch_size < 1:
        parser.error("Batch sizes must be positive")
    asyncio.run(run(
        dry_run=args.dry_run,
        revision_id=args.revision_id,
        encode_batch_size=args.encode_batch_size,
        fetch_batch_size=args.fetch_batch_size,
    ))

