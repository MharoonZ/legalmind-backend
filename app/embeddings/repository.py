"""Embedding repository: DB persistence and vector search.

Responsibilities
----------------
1. ``ensure_profile`` — idempotently create/fetch the ``embedding_profiles``
   row that matches a given ``EmbeddingProfile`` dataclass.
2. ``upsert_embeddings`` — persist a batch of (chunk_id, vector) pairs
   idempotently; safe to retry on partial failure.
3. ``vector_search`` — pgvector cosine similarity search scoped to one or
   more approved chunk sets.
4. ``embedding_stats`` — count how many chunks already have vectors for a
   given profile (useful for progress reporting).

Idempotency contract
--------------------
Every embedding row is keyed by ``(chunk_id, profile_id)``.  Re-running the
build script with the same inputs produces identical rows; no duplicates are
created.  If ``input_sha256`` matches the stored hash, the row is left
untouched.  If the input changed (e.g. because the search_text recipe was
corrected), the existing row is replaced.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.legal_corpus_models import (
    ChunkSet, CorpusRevision, LegalChunk, LegalInstrument, LegalVersion,
)
from app.db.session import Database
from app.embeddings.models import EmbeddingProfileModel, LegalChunkEmbedding
from app.embeddings.profiles import EmbeddingProfile
from app.services.legal_chunking import ChunkConfig

logger = logging.getLogger(__name__)


class EmbeddingRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    async def ensure_profile(self, profile: EmbeddingProfile) -> UUID:
        """Return the DB UUID for ``profile``, creating the row if absent.

        This is idempotent: calling it multiple times with the same profile is
        safe.  A mismatch in ``dimension`` for an existing name raises
        ``ValueError`` to prevent mixing incompatible vectors.
        """
        async with self.db.session() as session, session.begin():
            existing = (await session.execute(
                select(EmbeddingProfileModel).where(EmbeddingProfileModel.name == profile.name)
            )).scalar_one_or_none()

            if existing is not None:
                if existing.dimension != profile.dimension:
                    raise ValueError(
                        f"Profile '{profile.name}' already exists in the DB with dimension "
                        f"{existing.dimension}, but code defines dimension {profile.dimension}. "
                        "Create a new profile name if the dimension changed."
                    )
                return existing.id

            new_profile = EmbeddingProfileModel(
                name=profile.name,
                model_id=profile.model_id,
                dimension=profile.dimension,
                max_tokens=profile.max_tokens,
                input_recipe=profile.input_recipe,
                normalize=profile.normalize,
            )
            session.add(new_profile)
            await session.flush()
            logger.info(
                "Created embedding profile '%s' (id=%s, dim=%d).",
                profile.name, new_profile.id, profile.dimension,
            )
            return new_profile.id

    # ------------------------------------------------------------------
    # Fetching chunks that still need embeddings
    # ------------------------------------------------------------------

    async def chunks_without_embeddings(
        self,
        profile_id: UUID,
        *,
        revision_id: UUID | None = None,
        config: ChunkConfig | None = None,
        batch_size: int = 100,
    ) -> list[dict]:
        """Return chunks that do not yet have a vector for ``profile_id``.

        Returns a list of dicts with keys:
            chunk_id, revision_id, canonical_title, label, heading, chunk_text

        Parameters
        ----------
        revision_id:
            Limit to one revision (optional).
        config:
            Chunker config used to locate v2 chunk sets (defaults to current
            ``ChunkConfig()``).
        batch_size:
            Maximum number of rows returned per call.  Call repeatedly until
            an empty list is returned to process all chunks.
        """
        config = config or ChunkConfig()
        async with self.db.session() as session:
            # Sub-query: chunk_ids that already have this profile's vector
            embedded = select(LegalChunkEmbedding.chunk_id).where(
                LegalChunkEmbedding.profile_id == profile_id
            )
            query = (
                select(
                    LegalChunk.id.label("chunk_id"),
                    LegalChunk.revision_id,
                    LegalInstrument.canonical_title,
                    LegalChunk.label,
                    LegalChunk.heading,
                    LegalChunk.chunk_text,
                )
                .join(ChunkSet, ChunkSet.id == LegalChunk.chunk_set_id)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .where(
                    ChunkSet.chunker_version == config.chunker_version,
                    ChunkSet.config_sha256 == config.config_sha256,
                    ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                    CorpusRevision.review_state == "approved",
                    LegalChunk.id.notin_(embedded),
                )
                .order_by(LegalInstrument.canonical_title, LegalChunk.ordinal)
                .limit(batch_size)
            )
            if revision_id is not None:
                query = query.where(LegalChunk.revision_id == revision_id)

            rows = (await session.execute(query)).all()
            return [
                {
                    "chunk_id": row.chunk_id,
                    "revision_id": row.revision_id,
                    "canonical_title": row.canonical_title,
                    "label": row.label,
                    "heading": row.heading,
                    "chunk_text": row.chunk_text,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Persisting vectors
    # ------------------------------------------------------------------

    async def upsert_embeddings(
        self,
        profile_id: UUID,
        rows: list[dict],
    ) -> int:
        """Persist a batch of embedding vectors.

        Each element of ``rows`` must have:
            chunk_id (UUID), revision_id (UUID),
            input_sha256 (str), embedding (list[float])

        Uses PostgreSQL ON CONFLICT DO UPDATE so the call is safe to retry.
        Returns the number of rows upserted.
        """
        if not rows:
            return 0

        values = [
            {
                "chunk_id": r["chunk_id"],
                "profile_id": profile_id,
                "revision_id": r["revision_id"],
                "input_sha256": r["input_sha256"],
                "embedding": r["embedding"],
            }
            for r in rows
        ]

        async with self.db.session() as session, session.begin():
            stmt = pg_insert(LegalChunkEmbedding).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_lce_chunk_profile",
                set_={
                    "input_sha256": stmt.excluded.input_sha256,
                    "embedding": stmt.excluded.embedding,
                },
            )
            result = await session.execute(stmt)
            return result.rowcount

    # ------------------------------------------------------------------
    # Vector search
    # ------------------------------------------------------------------

    async def vector_search(
        self,
        query_vector: list[float],
        profile_id: UUID,
        *,
        revision_ids: list[UUID] | None = None,
        limit: int = 20,
        config: ChunkConfig | None = None,
    ) -> list[dict]:
        """Return the top-``limit`` chunks closest to ``query_vector``.

        Uses pgvector's ``<=>`` (cosine distance) operator.  Vectors must be
        L2-normalised (bge-m3 with ``normalize=True`` satisfies this).

        Results are scoped to approved v2 chunk sets.  If ``revision_ids`` is
        given, only those revisions are searched.

        Returns a list of dicts (same shape as keyword search results) with an
        additional ``vector_score`` key (cosine similarity, 0–1, higher is
        better).
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        config = config or ChunkConfig()

        # cosine distance via <=>; convert distance → similarity
        distance = LegalChunkEmbedding.embedding.op("<=>")(query_vector)

        async with self.db.session() as session:
            query = (
                select(
                    LegalChunk,
                    LegalInstrument.canonical_title,
                    LegalVersion.edition_key,
                    (1 - distance).label("vector_score"),
                )
                .join(LegalChunkEmbedding, LegalChunkEmbedding.chunk_id == LegalChunk.id)
                .join(ChunkSet, ChunkSet.id == LegalChunk.chunk_set_id)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .where(
                    LegalChunkEmbedding.profile_id == profile_id,
                    ChunkSet.chunker_version == config.chunker_version,
                    ChunkSet.config_sha256 == config.config_sha256,
                    ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                    CorpusRevision.review_state == "approved",
                )
                .order_by(distance)
                .limit(limit)
            )
            if revision_ids:
                query = query.where(ChunkSet.revision_id.in_(set(revision_ids)))

            rows = (await session.execute(query)).all()

        return [
            {
                "id": chunk.id,
                "chunk_set_id": chunk.chunk_set_id,
                "revision_id": chunk.revision_id,
                "root_provision_id": chunk.root_provision_id,
                "ordinal": chunk.ordinal,
                "kind": chunk.kind,
                "label": chunk.label,
                "heading": chunk.heading,
                "reference_path": chunk.reference_path,
                "chunk_key": chunk.chunk_key,
                "sequence_in_root": chunk.sequence_in_root,
                "root_total_chunks": chunk.root_total_chunks,
                "prefix": chunk.prefix,
                "chunk_text": chunk.chunk_text,
                "char_count": chunk.char_count,
                "instrument_title": title,
                "edition_key": edition,
                "vector_score": float(score),
            }
            for chunk, title, edition, score in rows
        ]

    # ------------------------------------------------------------------
    # Stats / progress
    # ------------------------------------------------------------------

    async def embedding_stats(
        self,
        profile_id: UUID,
        *,
        revision_id: UUID | None = None,
        config: ChunkConfig | None = None,
    ) -> dict:
        """Return total eligible chunks and how many are already embedded."""
        config = config or ChunkConfig()
        async with self.db.session() as session:
            base = (
                select(func.count(LegalChunk.id))
                .join(ChunkSet, ChunkSet.id == LegalChunk.chunk_set_id)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .where(
                    ChunkSet.chunker_version == config.chunker_version,
                    ChunkSet.config_sha256 == config.config_sha256,
                    ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                    CorpusRevision.review_state == "approved",
                )
            )
            if revision_id:
                base = base.where(LegalChunk.revision_id == revision_id)
            total = (await session.execute(base)).scalar_one()

            embedded_q = (
                select(func.count(LegalChunkEmbedding.chunk_id))
                .join(LegalChunk, LegalChunk.id == LegalChunkEmbedding.chunk_id)
                .join(ChunkSet, ChunkSet.id == LegalChunk.chunk_set_id)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .where(
                    LegalChunkEmbedding.profile_id == profile_id,
                    ChunkSet.chunker_version == config.chunker_version,
                    ChunkSet.config_sha256 == config.config_sha256,
                    ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                    CorpusRevision.review_state == "approved",
                )
            )
            if revision_id:
                embedded_q = embedded_q.where(LegalChunk.revision_id == revision_id)
            embedded = (await session.execute(embedded_q)).scalar_one()

        return {
            "total_eligible_chunks": total,
            "embedded_chunks": embedded,
            "remaining_chunks": total - embedded,
            "percent_done": round(100 * embedded / total, 1) if total else 100.0,
        }

