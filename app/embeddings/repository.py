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

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.legal_corpus_models import (
    ChunkSet, CorpusRevision, LegalChunk, LegalInstrument, LegalVersion,
)
from app.db.session import Database
from app.embeddings.models import EmbeddingProfileModel, LegalChunkEmbedding
from app.embeddings.profiles import EmbeddingProfile, assert_profile_matches
from app.embeddings.encoder import build_embedding_input, input_sha256, validate_vector
from app.services.legal_chunking import ChunkConfig

logger = logging.getLogger(__name__)


class EmbeddingRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_profile_id(self, profile: EmbeddingProfile) -> UUID | None:
        """Read-only resolution; audit/search/dry-run must never create profiles."""
        async with self.db.session() as session:
            stored = await session.scalar(select(EmbeddingProfileModel).where(
                EmbeddingProfileModel.name == profile.name))
            if stored is None:
                return None
            assert_profile_matches(stored, profile)
            return stored.id

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    async def ensure_profile(self, profile: EmbeddingProfile) -> UUID:
        """Return the DB UUID for ``profile``, creating the row if absent.

        Every persisted configuration field must match a reused profile name.
        Configuration changes require a new name, rather than mixed vectors.
        """
        async with self.db.session() as session, session.begin():
            await session.execute(pg_insert(EmbeddingProfileModel).values(
                name=profile.name, model_id=profile.model_id, dimension=profile.dimension,
                max_tokens=profile.max_tokens, input_recipe=profile.input_recipe,
                normalize=profile.normalize,
            ).on_conflict_do_nothing(index_elements=["name"]))
            stored = await session.scalar(select(EmbeddingProfileModel).where(
                EmbeddingProfileModel.name == profile.name))
            assert_profile_matches(stored, profile)
            return stored.id

    async def _embedding_candidates(self, profile_id: UUID, *, revision_id=None, config=None):
        """Inspect hashes and vector health without loading the model or vector payloads."""
        config = config or ChunkConfig()
        async with self.db.session() as session:
            stored = await session.get(EmbeddingProfileModel, profile_id)
            if stored is None:
                raise ValueError("Unknown embedding profile")
            profile = EmbeddingProfile(**{field: getattr(stored, field) for field in
                ("name", "model_id", "dimension", "max_tokens", "input_recipe", "normalize")})
            query = (select(LegalChunk.id.label("chunk_id"), LegalChunk.revision_id,
                LegalInstrument.canonical_title, LegalChunk.label, LegalChunk.heading,
                LegalChunk.chunk_text, LegalChunkEmbedding.input_sha256,
                func.vector_dims(LegalChunkEmbedding.embedding).label("dims"),
                func.vector_norm(LegalChunkEmbedding.embedding).label("norm"))
                .join(ChunkSet, ChunkSet.id == LegalChunk.chunk_set_id)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .outerjoin(LegalChunkEmbedding, (LegalChunkEmbedding.chunk_id == LegalChunk.id) &
                           (LegalChunkEmbedding.profile_id == profile_id))
                .where(ChunkSet.chunker_version == config.chunker_version,
                       ChunkSet.config_sha256 == config.config_sha256,
                       ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                       CorpusRevision.review_state == "approved")
                .order_by(LegalInstrument.canonical_title, LegalChunk.ordinal, LegalChunk.id))
            if revision_id is not None:
                query = query.where(LegalChunk.revision_id == revision_id)
            rows = (await session.execute(query)).mappings().all()
        result = []
        for row in rows:
            item = dict(row)
            item["expected_input_sha256"] = input_sha256(build_embedding_input(
                row["canonical_title"], row["label"], row["heading"], row["chunk_text"], profile))
            item["state"] = ("missing" if row["input_sha256"] is None else
                "stale" if row["input_sha256"] != item["expected_input_sha256"] else
                "invalid" if row["dims"] != profile.dimension or not row["norm"] or
                    (profile.normalize and abs(row["norm"] - 1) > .001) else "valid")
            result.append(item)
        return result

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
        """Return missing, stale, or invalid vectors for ``profile_id``.

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
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        rows = await self._embedding_candidates(profile_id, revision_id=revision_id, config=config)
        return [row for row in rows if row["state"] != "valid"][:batch_size]

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
            stored = await session.get(EmbeddingProfileModel, profile_id)
            if stored is None:
                raise ValueError("Unknown embedding profile")
            profile = EmbeddingProfile(**{field: getattr(stored, field) for field in
                ("name", "model_id", "dimension", "max_tokens", "input_recipe", "normalize")})
            for row in rows:
                validate_vector(row["embedding"], profile)
            # A BEFORE INSERT protection trigger runs before ON CONFLICT.
            # Skip healthy existing inputs first, so a valid published retry
            # remains a true no-op and never reaches a write trigger.
            existing = (await session.execute(select(
                LegalChunkEmbedding.chunk_id, LegalChunkEmbedding.revision_id,
                LegalChunkEmbedding.input_sha256,
                func.vector_dims(LegalChunkEmbedding.embedding).label("dimension"),
                func.vector_norm(LegalChunkEmbedding.embedding).label("norm"),
            ).where(LegalChunkEmbedding.profile_id == profile_id,
                    LegalChunkEmbedding.chunk_id.in_([r["chunk_id"] for r in rows])))).all()
            valid_keys = {(r.chunk_id, r.revision_id, r.input_sha256) for r in existing
                if r.dimension == profile.dimension and r.norm and
                   (not profile.normalize or abs(r.norm - 1) <= .001)}
            values = [r for r in values if (r["chunk_id"], r["revision_id"], r["input_sha256"]) not in valid_keys]
            if not values:
                return 0
            stmt = pg_insert(LegalChunkEmbedding).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["chunk_id", "profile_id"],
                set_={
                    "input_sha256": stmt.excluded.input_sha256,
                    "embedding": stmt.excluded.embedding,
                },
                where=or_(LegalChunkEmbedding.input_sha256 != stmt.excluded.input_sha256,
                          func.vector_norm(LegalChunkEmbedding.embedding) == 0,
                          func.abs(func.vector_norm(LegalChunkEmbedding.embedding) - 1) > .001
                          if profile.normalize else False),
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

        Uses pgvector's typed cosine-distance comparator. This profile stores
        L2-normalized vectors; cosine itself accepts any finite nonzero vector.

        Results are scoped to approved v2 chunk sets.  If ``revision_ids`` is
        given, only those revisions are searched.

        Returns a list of dicts (same shape as keyword search results) with an
        additional ``vector_score`` key (cosine similarity, -1–1, higher is
        better).
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        config = config or ChunkConfig()

        # cosine distance via <=>; convert distance → similarity
        distance = LegalChunkEmbedding.embedding.cosine_distance(query_vector)

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
            if revision_ids is not None:
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
        rows = await self._embedding_candidates(profile_id, revision_id=revision_id, config=config)
        total = len(rows)
        embedded = sum(row["state"] == "valid" for row in rows)
        return {
            "total_eligible_chunks": total,
            "embedded_chunks": embedded,
            "remaining_chunks": total - embedded,
            "missing_chunks": sum(row["state"] == "missing" for row in rows),
            "stale_chunks": sum(row["state"] == "stale" for row in rows),
            "invalid_chunks": sum(row["state"] == "invalid" for row in rows),
            "percent_done": round(100 * embedded / total, 1) if total else 100.0,
        }

