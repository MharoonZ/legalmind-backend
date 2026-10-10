"""Build and inspect legal-aware chunk sets for an approved corpus revision.

Mirrors the transactional/idempotency conventions already used by
``LegalStructureRepository``: a row lock on the parent record serializes
concurrent attempts, and a documented uniqueness constraint (here,
``(revision_id, content_sha256, chunker_version, config_sha256)``) is the
single source of truth for "did we already build this".
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import func, insert, select

from app.db.legal_corpus_models import (
    ChunkSet, CorpusRevision, LegalChunk, LegalChunkProvision, LegalInstrument,
    LegalVersion, Provision,
)
from app.db.session import Database
from app.services.legal_chunking import ChunkConfig, ProvisionNode, build_chunks


class ChunkingError(Exception):
    """The requested revision is not ready to be chunked, or has nothing to chunk."""


def _batches(items: list, size: int = 250):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class LegalChunkRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def build_for_revision(self, revision_id: UUID, *,
                                 config: ChunkConfig | None = None) -> dict:
        """Build (or idempotently reuse) a chunk set for one approved revision.

        Raises ``ChunkingError`` if the revision does not exist, is not
        approved, or has no content hash yet. Safe to call repeatedly: an
        unchanged revision with the same chunker version/config returns the
        existing chunk set without writing anything new.
        """
        config = config or ChunkConfig()
        async with self.db.session() as session, session.begin():
            revision = (await session.execute(
                select(CorpusRevision).where(CorpusRevision.id == revision_id).with_for_update()
            )).scalar_one_or_none()
            if revision is None:
                raise ChunkingError(f"Revision {revision_id} was not found.")
            if revision.review_state != "approved":
                raise ChunkingError(
                    f"Revision {revision_id} is '{revision.review_state}', not approved; "
                    "only an approved revision may be chunked."
                )
            if not revision.content_sha256:
                raise ChunkingError(f"Revision {revision_id} has no completed content hash.")

            existing = (await session.execute(select(ChunkSet).where(
                ChunkSet.revision_id == revision_id,
                ChunkSet.content_sha256 == revision.content_sha256,
                ChunkSet.chunker_version == config.chunker_version,
                ChunkSet.config_sha256 == config.config_sha256,
            ))).scalar_one_or_none()
            if existing is not None:
                return {
                    "chunk_set_id": existing.id, "revision_id": revision_id, "created": False,
                    "chunk_count": existing.chunk_report.get("chunk_count"),
                    "report": existing.chunk_report,
                }

            version = await session.get(LegalVersion, revision.legal_version_id)
            instrument = await session.get(LegalInstrument, version.instrument_id)
            provision_rows = (await session.execute(
                select(Provision).where(Provision.revision_id == revision_id)
            )).scalars().all()
            if not provision_rows:
                raise ChunkingError(f"Revision {revision_id} has no provisions to chunk.")

            provisions = [ProvisionNode(
                id=row.id, parent_id=row.parent_id, kind=row.kind, label=row.label,
                reference_path=row.reference_path, ordinal=row.ordinal, heading=row.heading,
                own_text=row.own_text, source_status=row.source_status,
            ) for row in provision_rows]
            result = build_chunks(instrument.canonical_title, provisions, config)
            if not result.chunks:
                raise ChunkingError(
                    f"Revision {revision_id} produced no chunkable provisions "
                    "(all excluded as 'other'/omitted/repealed/deleted, or blank)."
                )

            # Defense in depth: re-verify every chunk's text is an exact,
            # unmodified concatenation of its constituent provisions before
            # persisting anything (never trust a future refactor silently).
            node_by_id = {node.id: node for node in provisions}
            for chunk in result.chunks:
                body = "\n\n".join(
                    node_by_id[provision_id].own_text.strip()
                    for provision_id in chunk.provision_ids
                    if node_by_id[provision_id].own_text.strip()
                )
                if f"{chunk.prefix}\n\n{body}" != chunk.chunk_text:
                    raise ChunkingError(
                        "A chunk's text did not reproduce exactly from its source "
                        "provisions; refusing to persist a possibly-altered chunk."
                    )

            chunk_set_id = uuid4()
            session.add(ChunkSet(
                id=chunk_set_id, revision_id=revision_id,
                content_sha256=revision.content_sha256,
                chunker_version=config.chunker_version, config_sha256=config.config_sha256,
                chunk_report=result.report,
            ))
            await session.flush()

            chunk_ids = {chunk.ordinal: uuid4() for chunk in result.chunks}
            for batch in _batches(result.chunks):
                await session.execute(insert(LegalChunk).values([{
                    "id": chunk_ids[chunk.ordinal], "chunk_set_id": chunk_set_id,
                    "revision_id": revision_id, "root_provision_id": chunk.root_provision_id,
                    "ordinal": chunk.ordinal, "kind": chunk.kind, "label": chunk.label,
                    "heading": chunk.heading, "reference_path": chunk.reference_path,
                    "chunk_key": chunk.chunk_key, "sequence_in_root": chunk.sequence_in_root,
                    "root_total_chunks": chunk.root_total_chunks, "prefix": chunk.prefix,
                    "chunk_text": chunk.chunk_text, "char_count": chunk.char_count,
                    "chunk_text_sha256": chunk.chunk_text_sha256,
                    "search_text": chunk.search_text,
                } for chunk in batch]))

            link_rows = [{
                "chunk_id": chunk_ids[chunk.ordinal], "revision_id": revision_id,
                "provision_id": provision_id, "ordinal": position,
            } for chunk in result.chunks
              for position, provision_id in enumerate(chunk.provision_ids, start=1)]
            for batch in _batches(link_rows):
                await session.execute(insert(LegalChunkProvision).values(batch))

            return {
                "chunk_set_id": chunk_set_id, "revision_id": revision_id, "created": True,
                "chunk_count": len(result.chunks), "report": result.report,
            }

    async def latest_chunk_set(self, revision_id: UUID) -> dict | None:
        async with self.db.session() as session:
            chunk_set = (await session.execute(
                select(ChunkSet).where(ChunkSet.revision_id == revision_id)
                .order_by(ChunkSet.created_at.desc())
            )).scalars().first()
            if chunk_set is None:
                return None
            return {
                "chunk_set_id": chunk_set.id, "revision_id": revision_id,
                "content_sha256": chunk_set.content_sha256,
                "chunker_version": chunk_set.chunker_version,
                "config_sha256": chunk_set.config_sha256,
                "chunk_report": chunk_set.chunk_report,
                "created_at": chunk_set.created_at,
            }

    async def chunk_set_overview(self, chunk_set_id: UUID) -> dict | None:
        """Chunks in document order with their exact provision membership."""
        async with self.db.session() as session:
            chunk_set = await session.get(ChunkSet, chunk_set_id)
            if chunk_set is None:
                return None
            chunks = (await session.execute(
                select(LegalChunk).where(LegalChunk.chunk_set_id == chunk_set_id)
                .order_by(LegalChunk.ordinal)
            )).scalars().all()
            links = (await session.execute(
                select(LegalChunkProvision)
                .where(LegalChunkProvision.chunk_id.in_([chunk.id for chunk in chunks]))
                .order_by(LegalChunkProvision.chunk_id, LegalChunkProvision.ordinal)
            )).scalars().all() if chunks else []
            provisions_by_chunk: dict[UUID, list[UUID]] = {}
            for link in links:
                provisions_by_chunk.setdefault(link.chunk_id, []).append(link.provision_id)
            return {
                "chunk_set_id": chunk_set.id, "revision_id": chunk_set.revision_id,
                "chunk_report": chunk_set.chunk_report,
                "chunks": [{
                    "id": chunk.id, "ordinal": chunk.ordinal, "kind": chunk.kind,
                    "label": chunk.label, "heading": chunk.heading,
                    "reference_path": chunk.reference_path, "chunk_key": chunk.chunk_key,
                    "sequence_in_root": chunk.sequence_in_root,
                    "root_total_chunks": chunk.root_total_chunks,
                    "prefix": chunk.prefix, "chunk_text": chunk.chunk_text,
                    "char_count": chunk.char_count,
                    "chunk_text_sha256": chunk.chunk_text_sha256,
                    "provision_ids": provisions_by_chunk.get(chunk.id, []),
                } for chunk in chunks],
            }

    @staticmethod
    def _chunk_summary(chunk: LegalChunk) -> dict:
        return {
            "id": chunk.id, "chunk_set_id": chunk.chunk_set_id, "ordinal": chunk.ordinal,
            "kind": chunk.kind, "label": chunk.label, "heading": chunk.heading,
            "reference_path": chunk.reference_path, "chunk_key": chunk.chunk_key,
            "sequence_in_root": chunk.sequence_in_root,
            "root_total_chunks": chunk.root_total_chunks,
            "prefix": chunk.prefix, "chunk_text": chunk.chunk_text,
            "char_count": chunk.char_count,
        }

    async def find_by_label(self, chunk_set_id: UUID, label: str) -> list[dict]:
        """Exact, unstemmed lookup by printed label (e.g. "19A") within one
        chunk set.

        A plain equality match, never a prefix or substring match: looking
        up ``"19"`` never also returns ``"19A"`` (and vice versa), because
        those are different ``label`` values entirely. Uses
        ``ix_legal_chunks_chunk_set_label``.
        """
        async with self.db.session() as session:
            chunks = (await session.execute(
                select(LegalChunk).where(
                    LegalChunk.chunk_set_id == chunk_set_id,
                    LegalChunk.label == label,
                ).order_by(LegalChunk.sequence_in_root)
            )).scalars().all()
            return [self._chunk_summary(chunk) for chunk in chunks]

    async def keyword_search(self, chunk_set_id: UUID, query: str, *, limit: int = 20) -> list[dict]:
        """Rank one chunk set's chunks by PostgreSQL full-text search over
        ``search_vector`` (the GIN-indexed ``tsvector`` generated from
        ``search_text``: Act title, section label, heading and body).

        ``plainto_tsquery`` treats a plain question like "what does section
        19A say" as an AND of its words rather than requiring ``tsquery``
        syntax from the caller. This is lexical search only; it does not
        replace an exact-reference lookup (``find_by_label``) for a query
        that names a precise section number.
        """
        if limit < 1:
            raise ValueError("limit must be positive.")
        tsquery = func.plainto_tsquery("english", query)
        rank = func.ts_rank(LegalChunk.search_vector, tsquery).label("rank")
        async with self.db.session() as session:
            rows = (await session.execute(
                select(LegalChunk, rank).where(
                    LegalChunk.chunk_set_id == chunk_set_id,
                    LegalChunk.search_vector.op("@@")(tsquery),
                ).order_by(rank.desc()).limit(limit)
            )).all()
            return [{**self._chunk_summary(chunk), "rank": float(score)} for chunk, score in rows]
