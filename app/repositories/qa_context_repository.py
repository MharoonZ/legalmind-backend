"""Read-only, publication-pinned source graph for answer context expansion."""
from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, text, true, tuple_
from sqlalchemy.orm import load_only

from app.db.legal_corpus_models import (
    ChunkSet, CorpusRevision, FileBlock, FileExtraction, FilePage, LegalChunk,
    LegalChunkProvision, LegalFile, LegalInstrument, LegalVersion, Jurisdiction,
    Provision, ProvisionReference, ProvisionSpan,
    RevisionSource,
)
from app.db.publication_models import CorpusPublication, PublicationItem
from app.repositories.legal_chunk_repository import LegalChunkRepository
from app.services.qa.source_proofs import valid_text_range, original_source_fragment


class ContextScopeError(RuntimeError):
    """The retrieved publication or approved source scope is no longer usable."""


def source_read_options():
    """Read proof fields without repeating full pages/extraction reports per span."""
    return (
        load_only(Provision.id, Provision.revision_id, Provision.own_text, Provision.text_sha256,
                  Provision.reference_path, Provision.kind, Provision.label),
        load_only(FilePage.id, FilePage.page_index),
        load_only(FileBlock.id, FileBlock.raw_text, FileBlock.normalized_text, FileBlock.text_sha256),
        load_only(FileExtraction.id, FileExtraction.file_id),
        load_only(LegalFile.id, LegalFile.sha256, LegalFile.original_filename, LegalFile.source_url, LegalFile.publisher),
        load_only(RevisionSource.revision_id, RevisionSource.extraction_id,
                  RevisionSource.file_id, RevisionSource.source_role),
    )


@dataclass(frozen=True)
class PublishedItem:
    instrument_id: UUID
    legal_version_id: UUID
    revision_id: UUID
    chunk_set_id: UUID
    content_sha256: str
    instrument_title: str
    citation_key: str
    edition_key: str
    jurisdiction_code: str
    applicability_status: str


class ContextCatalog:
    def __init__(self, session, items, chunks, nodes, members, references, *, loader=None):
        self.session = session
        self.items = items
        self.chunks = chunks
        self.nodes = nodes
        self.members = members
        self.references = references
        self._loader = loader
        self._loaded_instruments = set()
        # Caches live only for this transaction, never across questions or LLM calls.
        self._chunk_sources = {}
        self._provision_sources = {}
        self._index_graph()

    def _index_graph(self):
        self.chunks_by_root = defaultdict(list)
        self.chunks_by_provision = defaultdict(list)
        self.nodes_by_path = {}
        self.children = defaultdict(list)
        for chunk in self.chunks.values():
            self.chunks_by_root[(chunk.revision_id, chunk.root_provision_id)].append(chunk)
        for rows in self.chunks_by_root.values():
            rows.sort(key=lambda chunk: chunk.sequence_in_root)
        for chunk_id, provision_ids in self.members.items():
            for provision_id in provision_ids:
                self.chunks_by_provision[provision_id].append(self.chunks[chunk_id])
        for node in self.nodes.values():
            self.nodes_by_path[(node.revision_id, node.reference_path)] = node
            if node.parent_id:
                self.children[node.parent_id].append(node)
        for rows in self.children.values():
            rows.sort(key=lambda node: node.ordinal)

    async def ensure_instruments(self, instrument_ids):
        """Load a referenced law only when its published graph is needed."""
        missing = set(instrument_ids) - self._loaded_instruments
        if missing and self._loader is not None:
            if not missing <= self.items.keys():
                raise ContextScopeError("Context requested an unpublished instrument.")
            await self._loader(self, missing)
            self._loaded_instruments.update(missing)
            self._index_graph()

    async def chunk_sources(self, chunk_ids):
        missing = set(chunk_ids) - self._chunk_sources.keys()
        if missing:
            selected = [self.chunks[chunk_id] for chunk_id in missing]
            rows = await LegalChunkRepository._source_refs(self.session, selected)
            self._chunk_sources.update({key: rows.get(key, []) for key in missing})
        return {key: self._chunk_sources[key] for key in chunk_ids}

    async def provision_sources(self, provision_ids):
        missing = set(provision_ids) - self._provision_sources.keys()
        if missing:
            rows = await self._read_provision_sources(missing)
            self._provision_sources.update({key: rows.get(key, []) for key in missing})
        return {key: self._provision_sources[key] for key in provision_ids}

    async def _read_provision_sources(self, provision_ids):
        if not provision_ids:
            return {}
        rows = (await self.session.execute(
            select(ProvisionSpan, Provision, FilePage, FileBlock, FileExtraction,
                   LegalFile, RevisionSource)
            .options(*source_read_options())
            .join(Provision, (Provision.id == ProvisionSpan.provision_id) &
                 (Provision.revision_id == ProvisionSpan.revision_id))
            .join(RevisionSource, (RevisionSource.revision_id == ProvisionSpan.revision_id) &
                 (RevisionSource.extraction_id == ProvisionSpan.extraction_id))
            .join(FilePage, (FilePage.id == ProvisionSpan.page_id) &
                 (FilePage.extraction_id == ProvisionSpan.extraction_id))
            .join(FileBlock, (FileBlock.id == ProvisionSpan.block_id) &
                 (FileBlock.page_id == FilePage.id))
            .join(FileExtraction, FileExtraction.id == ProvisionSpan.extraction_id)
            .join(LegalFile, (LegalFile.id == FileExtraction.file_id) &
                 (LegalFile.id == RevisionSource.file_id))
            .where(ProvisionSpan.provision_id.in_(provision_ids))
            .order_by(ProvisionSpan.provision_id, ProvisionSpan.provision_char_start)
        )).all()
        result = defaultdict(list)
        for span, node, page, block, extraction, file, source in rows:
            if (span.mapping_quality != "exact" or
                not valid_text_range(node.own_text, span.provision_char_start, span.provision_char_end) or
                not valid_text_range(block.normalized_text, span.block_char_start, span.block_char_end) or
                node.own_text[span.provision_char_start:span.provision_char_end] !=
                block.normalized_text[span.block_char_start:span.block_char_end]):
                raise ContextScopeError("Published provision has non-exact or changed source text.")
            result[node.id].append({
                "provision_id": node.id, "source_span_id": span.id,
                "extraction_id": extraction.id, "file_id": file.id,
                "file_sha256": file.sha256, "filename": file.original_filename,
                "source_url": file.source_url, "publisher": file.publisher,
                "source_role": source.source_role,
                "reference_path": node.reference_path, "kind": node.kind,
                "label": node.label, "mapping_quality": "exact",
                "page_id": page.id, "page_index": page.page_index,
                "block_id": block.id, "block_text_sha256": block.text_sha256,
                "provision_text_sha256": node.text_sha256,
                "provision_char_start": span.provision_char_start,
                "provision_char_end": span.provision_char_end,
                "block_char_start": span.block_char_start,
                "block_char_end": span.block_char_end,
                "original_text": original_source_fragment(block.raw_text,block.normalized_text,
                    span.block_char_start,span.block_char_end),
            })
        for provision_id in provision_ids:
            node = self.nodes[provision_id]
            spans = result.get(provision_id, [])
            cursor = 0
            for row in spans:
                if row["provision_char_start"] < cursor or node.own_text[cursor:row["provision_char_start"]].strip():
                    raise ContextScopeError("Published provision has incomplete source spans.")
                cursor = row["provision_char_end"]
            if node.own_text[cursor:].strip():
                raise ContextScopeError("Published provision has incomplete source spans.")
        return dict(result)


class QaContextRepository:
    """Uses the application's shared DB pool; every read is in one read-only snapshot."""
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def _catalog(self, publication_id):
        async with self.db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            actual_chunks = (select(func.count()).select_from(LegalChunk)
                .join(PublicationItem, (PublicationItem.chunk_set_id == LegalChunk.chunk_set_id) &
                      (PublicationItem.revision_id == LegalChunk.revision_id))
                .where(PublicationItem.publication_id == publication_id).scalar_subquery())
            # Project identity fields instead of fetching large revision manifests,
            # publication reports and other documents unused by question answering.
            sources = (select(
                PublicationItem.instrument_id, PublicationItem.legal_version_id,
                PublicationItem.revision_id, PublicationItem.chunk_set_id,
                PublicationItem.content_sha256,
                CorpusRevision.review_state,
                CorpusRevision.legal_version_id.label("revision_version"),
                CorpusRevision.content_sha256.label("revision_hash"),
                ChunkSet.revision_id.label("set_revision"),
                ChunkSet.content_sha256.label("set_hash"),
                LegalInstrument.canonical_title, LegalInstrument.citation_key,
                LegalVersion.edition_key, Jurisdiction.code, LegalVersion.applicability_status)
                .select_from(PublicationItem)
                .join(CorpusRevision, CorpusRevision.id == PublicationItem.revision_id)
                .join(ChunkSet, ChunkSet.id == PublicationItem.chunk_set_id)
                .join(LegalVersion, LegalVersion.id == PublicationItem.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == PublicationItem.instrument_id)
                .join(Jurisdiction, Jurisdiction.id == LegalInstrument.jurisdiction_id)
                .where(PublicationItem.publication_id == publication_id).subquery("published_sources"))
            # One statement checks the live publication and reads exactly the
            # same pinned identities. The outer join retains the empty-publication
            # case so it still fails closed, rather than disappearing in a join.
            item_rows = (await session.execute(select(
                CorpusPublication.chunk_count, actual_chunks.label("actual_chunks"),
                *sources.c)
                .select_from(CorpusPublication).outerjoin(sources, true())
                .where(CorpusPublication.id == publication_id,
                       CorpusPublication.status == "active"))).all()
            if not item_rows:
                raise ContextScopeError("The selected publication is no longer active.")
            publication = item_rows[0]
            if publication.chunk_count != publication.actual_chunks:
                raise ContextScopeError("Published chunks are incomplete.")
            if publication.instrument_id is None:
                raise ContextScopeError("The selected publication has no pinned sources.")
            items = {}
            for item in item_rows:
                if (item.review_state != "approved" or
                    item.revision_version != item.legal_version_id or
                    item.revision_hash != item.content_sha256 or
                    item.set_revision != item.revision_id or
                    item.set_hash != item.content_sha256):
                    raise ContextScopeError("Publication source identity changed.")
                items[item.instrument_id] = PublishedItem(
                    item.instrument_id, item.legal_version_id, item.revision_id,
                    item.chunk_set_id, item.content_sha256,
                    item.canonical_title, item.citation_key, item.edition_key,
                    item.code, item.applicability_status)
            yield ContextCatalog(session, items, {}, {}, defaultdict(set),
                                 defaultdict(list), loader=self._load_instruments)

    @staticmethod
    def _chunks():
        # Do not transfer the duplicate search text/vector on answer-context reads.
        return select(LegalChunk).options(load_only(
            LegalChunk.id, LegalChunk.chunk_set_id, LegalChunk.revision_id,
            LegalChunk.root_provision_id, LegalChunk.ordinal, LegalChunk.kind,
            LegalChunk.label, LegalChunk.heading, LegalChunk.reference_path,
            LegalChunk.sequence_in_root, LegalChunk.root_total_chunks,
            LegalChunk.chunk_text, raiseload=True))

    @staticmethod
    def _scope(catalog):
        return tuple_(LegalChunk.chunk_set_id, LegalChunk.revision_id).in_(
            [(item.chunk_set_id, item.revision_id) for item in catalog.items.values()])

    async def _load_instruments(self, catalog, instrument_ids):
        items = [catalog.items[key] for key in instrument_ids]
        revisions = [item.revision_id for item in items]
        # Membership is a set, so aggregate it beside each chunk. This avoids
        # another round trip and does not repeat chunk text once per source span.
        member_ids = (select(func.array_agg(LegalChunkProvision.provision_id.distinct()))
            .where(LegalChunkProvision.chunk_id == LegalChunk.id)
            .correlate(LegalChunk).scalar_subquery())
        chunk_rows = (await catalog.session.execute(self._chunks().add_columns(
            member_ids.label("member_ids")).where(
            tuple_(LegalChunk.chunk_set_id, LegalChunk.revision_id).in_(
                [(item.chunk_set_id, item.revision_id) for item in items]))
            .order_by(LegalChunk.ordinal))).all()
        chunks = [chunk for chunk, _ in chunk_rows]
        nodes = (await catalog.session.scalars(select(Provision)
            .where(Provision.revision_id.in_(revisions)).order_by(Provision.ordinal))).all()
        catalog.chunks.update({row.id: row for row in chunks})
        catalog.nodes.update({row.id: row for row in nodes})
        for chunk, provision_ids in chunk_rows:
            for provision_id in provision_ids or ():
                node = catalog.nodes.get(provision_id)
                if node is None or node.revision_id != chunk.revision_id:
                    raise ContextScopeError("Chunk membership crosses a published revision.")
                catalog.members[chunk.id].add(provision_id)
        references = (await catalog.session.scalars(select(ProvisionReference)
            .join(Provision, Provision.id == ProvisionReference.source_provision_id)
            .where(Provision.revision_id.in_(revisions)))).all()
        for row in references:
            catalog.references[row.source_provision_id].append(row)

    @asynccontextmanager
    async def snapshot(self, publication_id, *, instrument_ids=None):
        async with self._catalog(publication_id) as catalog:
            await catalog.ensure_instruments(catalog.items if instrument_ids is None else instrument_ids)
            yield catalog

    @asynccontextmanager
    async def proof_snapshot(self, publication_id, *, chunk_ids, provision_ids):
        """A fresh post-generation view of only the evidence being quoted."""
        async with self._catalog(publication_id) as catalog:
            if chunk_ids:
                rows = (await catalog.session.scalars(self._chunks().where(
                    self._scope(catalog), LegalChunk.id.in_(chunk_ids)))).all()
                catalog.chunks.update({row.id: row for row in rows})
            if provision_ids:
                rows = (await catalog.session.scalars(select(Provision).where(
                    Provision.revision_id.in_([item.revision_id for item in catalog.items.values()]),
                    Provision.id.in_(provision_ids)))).all()
                catalog.nodes.update({row.id: row for row in rows})
            yield catalog
