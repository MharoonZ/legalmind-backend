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
    ChunkSet, CorpusRevision, FileBlock, FileExtraction, FilePage, LegalChunk,
    LegalChunkProvision, LegalFile, LegalInstrument, LegalVersion, Provision, ProvisionSpan,
)
from app.db.session import Database
from app.services.legal_chunking import (
    ChunkConfig, ProvisionNode, ProvisionSourceSpan, build_chunks_from_spans,
)


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
                counts = (await session.execute(
                    select(func.count(func.distinct(LegalChunk.id)),
                           func.count(LegalChunkProvision.source_span_id))
                    .select_from(LegalChunk)
                    .outerjoin(LegalChunkProvision, LegalChunkProvision.chunk_id == LegalChunk.id)
                    .where(LegalChunk.chunk_set_id == existing.id)
                )).one()
                if counts[0] != existing.chunk_report.get("chunk_count") or (
                    existing.chunk_report.get("format") == "source_fragments_v2" and
                    counts[1] != existing.chunk_report.get("source_fragments")
                ):
                    raise ChunkingError(f"Existing chunk set {existing.id} is incomplete; audit before rebuilding")
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
            span_rows = (await session.execute(
                select(ProvisionSpan, FilePage.page_index, FileBlock.block_ordinal,
                       FileBlock.normalized_text)
                .join(FilePage, FilePage.id == ProvisionSpan.page_id)
                .outerjoin(FileBlock, FileBlock.id == ProvisionSpan.block_id)
                .where(ProvisionSpan.revision_id == revision_id)
            )).all()
            source_spans = []
            nodes = {node.id: node for node in provisions}
            for span, page_index, block_ordinal, block_text in span_rows:
                node = nodes.get(span.provision_id)
                if node is None:
                    raise ChunkingError(f"Source span {span.id} has no revision provision")
                if span.mapping_quality == "exact":
                    source_slice = (block_text or "")[span.block_char_start:span.block_char_end]
                    provision_slice = node.own_text[span.provision_char_start:span.provision_char_end]
                    if source_slice != provision_slice:
                        raise ChunkingError(
                            f"Source span {span.id} differs from the saved block; refusing to chunk")
                source_spans.append(ProvisionSourceSpan(
                    id=span.id, provision_id=span.provision_id,
                    extraction_id=span.extraction_id, page_index=page_index,
                    block_ordinal=block_ordinal or 0,
                    block_char_start=span.block_char_start or 0,
                    provision_char_start=span.provision_char_start,
                    provision_char_end=span.provision_char_end,
                    mapping_quality=span.mapping_quality,
                ))
            try:
                result = build_chunks_from_spans(
                    instrument.canonical_title, provisions, source_spans, config)
            except ValueError as exc:
                raise ChunkingError(str(exc)) from exc
            if not result.chunks:
                raise ChunkingError(
                    f"Revision {revision_id} produced no chunkable provisions "
                    "(all excluded as 'other'/omitted/repealed/deleted, or blank)."
                )

            # Every v2 membership is a specific, reproducible source fragment.
            for chunk in result.chunks:
                if not chunk.segments:
                    raise ChunkingError("Chunk has no source fragments")
                reconstructed = chunk.prefix + "\n\n"
                previous_segment = None
                for segment in chunk.segments:
                    piece = nodes[segment.provision_id].own_text[
                        segment.provision_char_start:segment.provision_char_end]
                    if chunk.chunk_text[segment.chunk_char_start:segment.chunk_char_end] != piece:
                        raise ChunkingError("Chunk fragment does not match saved provision text")
                    if previous_segment is not None and not (
                        previous_segment.provision_id == segment.provision_id and
                        previous_segment.provision_char_end == segment.provision_char_start
                    ):
                        reconstructed += "\n\n"
                    reconstructed += piece
                    previous_segment = segment
                if reconstructed != chunk.chunk_text:
                    raise ChunkingError("Chunk has text outside its source fragments")

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
                "provision_id": segment.provision_id, "ordinal": position,
                "source_span_id": segment.source_span_id,
                "provision_char_start": segment.provision_char_start,
                "provision_char_end": segment.provision_char_end,
                "chunk_char_start": segment.chunk_char_start,
                "chunk_char_end": segment.chunk_char_end,
            } for chunk in result.chunks
              for position, segment in enumerate(chunk.segments, start=1)]
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
                .order_by(ChunkSet.created_at.desc(), ChunkSet.id.desc())
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
            segments_by_chunk: dict[UUID, list[dict]] = {}
            for link in links:
                provisions_by_chunk.setdefault(link.chunk_id, []).append(link.provision_id)
                segments_by_chunk.setdefault(link.chunk_id, []).append({
                    "provision_id": link.provision_id,
                    "source_span_id": link.source_span_id,
                    "provision_char_start": link.provision_char_start,
                    "provision_char_end": link.provision_char_end,
                    "chunk_char_start": link.chunk_char_start,
                    "chunk_char_end": link.chunk_char_end,
                })
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
                    "provision_ids": list(dict.fromkeys(provisions_by_chunk.get(chunk.id, []))),
                    "segments": segments_by_chunk.get(chunk.id, []),
                } for chunk in chunks],
            }

    @staticmethod
    def _chunk_summary(chunk: LegalChunk) -> dict:
        return {
            "id": chunk.id, "chunk_set_id": chunk.chunk_set_id,
            "revision_id": chunk.revision_id,
            "root_provision_id": chunk.root_provision_id, "ordinal": chunk.ordinal,
            "kind": chunk.kind, "label": chunk.label, "heading": chunk.heading,
            "reference_path": chunk.reference_path, "chunk_key": chunk.chunk_key,
            "sequence_in_root": chunk.sequence_in_root,
            "root_total_chunks": chunk.root_total_chunks,
            "prefix": chunk.prefix, "chunk_text": chunk.chunk_text,
            "char_count": chunk.char_count,
        }

    @staticmethod
    async def _is_searchable(session, chunk_set_id: UUID) -> bool:
        return (await session.execute(
            select(ChunkSet.id).join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
            .where(ChunkSet.id == chunk_set_id,
                   ChunkSet.chunker_version == ChunkConfig().chunker_version,
                   ChunkSet.config_sha256 == ChunkConfig().config_sha256,
                   CorpusRevision.review_state == "approved",
                   CorpusRevision.content_sha256 == ChunkSet.content_sha256)
        )).scalar_one_or_none() is not None

    @staticmethod
    async def _source_refs(session, chunks: list[LegalChunk]) -> dict[UUID, list[dict]]:
        return await LegalChunkRepository.source_refs_for_ids(session, [chunk.id for chunk in chunks])

    @staticmethod
    async def source_refs_for_ids(session, chunk_ids: list[UUID]) -> dict[UUID, list[dict]]:
        if not chunk_ids:
            return {}
        rows = (await session.execute(
            select(LegalChunkProvision, ProvisionSpan.page_id, ProvisionSpan.block_id,
                   FilePage.page_index, ProvisionSpan.extraction_id,
                   Provision.text_sha256, FileBlock.text_sha256, LegalFile.id,
                   LegalFile.sha256, LegalFile.original_filename, LegalFile.source_url,
                   LegalFile.publisher, Provision.reference_path, Provision.kind,
                   Provision.label, ProvisionSpan.mapping_quality)
            .outerjoin(ProvisionSpan, ProvisionSpan.id == LegalChunkProvision.source_span_id)
            .outerjoin(FilePage, FilePage.id == ProvisionSpan.page_id)
            .join(Provision, Provision.id == LegalChunkProvision.provision_id)
            .outerjoin(FileBlock, FileBlock.id == ProvisionSpan.block_id)
            .outerjoin(FileExtraction, FileExtraction.id == ProvisionSpan.extraction_id)
            .outerjoin(LegalFile, LegalFile.id == FileExtraction.file_id)
            .where(LegalChunkProvision.chunk_id.in_(chunk_ids))
            .order_by(LegalChunkProvision.chunk_id, LegalChunkProvision.ordinal)
        )).all()
        result: dict[UUID, list[dict]] = {}
        for (link, page_id, block_id, page_index, extraction_id,
             provision_hash, block_hash, file_id, file_hash, filename, source_url,
             publisher, provision_path, provision_kind, provision_label, mapping_quality) in rows:
            result.setdefault(link.chunk_id, []).append({
                "provision_id": link.provision_id,
                "source_span_id": link.source_span_id,
                "extraction_id": extraction_id,
                "file_id": file_id,
                "file_sha256": file_hash,
                "filename": filename,
                "source_url": source_url,
                "publisher": publisher,
                "reference_path": provision_path,
                "kind": provision_kind,
                "label": provision_label,
                "mapping_quality": mapping_quality,
                "page_id": page_id,
                "page_index": page_index,
                "block_id": block_id,
                "block_text_sha256": block_hash,
                "provision_text_sha256": provision_hash,
                "provision_char_start": link.provision_char_start,
                "provision_char_end": link.provision_char_end,
                "chunk_char_start": link.chunk_char_start,
                "chunk_char_end": link.chunk_char_end,
            })
        return result

    async def find_by_label(self, chunk_set_id: UUID, label: str, *,
                            kind: str | None = None) -> list[dict]:
        """Unstemmed root-label lookup within one approved v2 chunk set.

        A plain equality match, never a prefix or substring match: looking
        up ``"19"`` never also returns ``"19A"``. Supply ``kind="section"``
        (or rule/article/schedule) when the same law has mixed root kinds. Uses
        ``ix_legal_chunks_chunk_set_label``.
        """
        if not label.strip():
            return []
        if kind is not None and kind not in {"preamble", "section", "rule", "article", "schedule"}:
            raise ValueError("Unknown legal root kind")
        async with self.db.session() as session:
            if not await self._is_searchable(session, chunk_set_id):
                return []
            predicates = [LegalChunk.chunk_set_id == chunk_set_id,
                          LegalChunk.label == label]
            if kind is not None:
                predicates.append(LegalChunk.kind == kind)
            chunks = (await session.execute(
                select(LegalChunk).where(*predicates)
                .order_by(LegalChunk.ordinal)
            )).scalars().all()
            sources = await self._source_refs(session, chunks)
            return [{**self._chunk_summary(chunk), "source_refs": sources.get(chunk.id, [])}
                    for chunk in chunks]

    async def find_by_reference_path(self, chunk_set_id: UUID, reference_path: str) -> list[dict]:
        """Resolve an exact stored provision path, including child labels, in one law edition.

        The caller must first select the correct law/edition and resolve the
        user's printed reference to a stored path. No prefix matching is used.
        """
        if not reference_path.strip():
            return []
        async with self.db.session() as session:
            if not await self._is_searchable(session, chunk_set_id):
                return []
            chunks = (await session.execute(
                select(LegalChunk)
                .join(LegalChunkProvision, LegalChunkProvision.chunk_id == LegalChunk.id)
                .join(Provision, Provision.id == LegalChunkProvision.provision_id)
                .where(LegalChunk.chunk_set_id == chunk_set_id,
                       Provision.reference_path == reference_path)
                .distinct().order_by(LegalChunk.ordinal)
            )).scalars().all()
            sources = await self._source_refs(session, chunks)
            return [{**self._chunk_summary(chunk), "source_refs": sources.get(chunk.id, [])}
                    for chunk in chunks]

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
        if not query.strip():
            return []
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        tsquery = func.plainto_tsquery("english", query)
        rank = (
            func.ts_rank(LegalChunk.search_vector, tsquery) +
            func.ts_rank(func.to_tsvector("english", func.coalesce(LegalChunk.heading, "")), tsquery)
        ).label("rank")
        async with self.db.session() as session:
            if not await self._is_searchable(session, chunk_set_id):
                return []
            rows = (await session.execute(
                select(LegalChunk, rank).where(
                    LegalChunk.chunk_set_id == chunk_set_id,
                    LegalChunk.search_vector.op("@@")(tsquery),
                ).order_by(rank.desc(), LegalChunk.ordinal).limit(limit)
            )).all()
            sources = await self._source_refs(session, [chunk for chunk, _ in rows])
            return [{**self._chunk_summary(chunk), "rank": float(score),
                     "source_refs": sources.get(chunk.id, [])} for chunk, score in rows]

    async def keyword_search_revisions(
        self, revision_ids: list[UUID], query: str, *, config: ChunkConfig | None = None,
        limit: int = 20, dedupe_roots: bool = True,
    ) -> list[dict]:
        """Search an explicitly selected approved corpus, with no category input.

        The publication/edition policy belongs to the caller. This method
        never chooses a legally current edition, never mixes chunker builds,
        and returns only approved revisions whose content hash still matches.
        """
        if not query.strip() or not revision_ids:
            return []
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        config = config or ChunkConfig()
        tsquery = func.plainto_tsquery("english", query)
        rank = (
            func.ts_rank(LegalChunk.search_vector, tsquery) +
            3 * func.ts_rank(func.to_tsvector("english", LegalInstrument.canonical_title), tsquery) +
            func.ts_rank(func.to_tsvector("english", func.coalesce(LegalChunk.heading, "")), tsquery)
        ).label("rank")
        async with self.db.session() as session:
            rows = (await session.execute(
                select(LegalChunk, rank, LegalInstrument.canonical_title,
                       LegalVersion.edition_key)
                .join(ChunkSet, ChunkSet.id == LegalChunk.chunk_set_id)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .where(ChunkSet.revision_id.in_(set(revision_ids)),
                       ChunkSet.chunker_version == config.chunker_version,
                       ChunkSet.config_sha256 == config.config_sha256,
                       ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                       CorpusRevision.review_state == "approved",
                       LegalChunk.search_vector.op("@@")(tsquery))
                .order_by(rank.desc(), LegalInstrument.canonical_title,
                          LegalChunk.ordinal)
                .limit(min(limit * 10, 1000) if dedupe_roots else limit)
            )).all()
            if dedupe_roots:
                selected = []
                seen_roots = set()
                for row in rows:
                    key = (row[0].revision_id, row[0].root_provision_id)
                    if key in seen_roots:
                        continue
                    selected.append(row)
                    seen_roots.add(key)
                    if len(selected) == limit:
                        break
                rows = selected
            sources = await self._source_refs(session, [chunk for chunk, *_ in rows])
            return [{**self._chunk_summary(chunk), "rank": float(score),
                     "instrument_title": title, "edition_key": edition,
                     "source_refs": sources.get(chunk.id, [])}
                    for chunk, score, title, edition in rows]
