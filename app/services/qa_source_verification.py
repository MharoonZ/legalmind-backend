"""Read-only proof that quoted characters map to the pinned PDF extraction."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from app.db.legal_corpus_models import (
    FileBlock, FileExtraction, FilePage, LegalChunkProvision, LegalFile,
    Provision, ProvisionSpan, RevisionSource,
)
from app.repositories.qa_context_repository import QaContextRepository
from app.schemas.qa_context import ContextUnit
from app.services.qa_source_proofs import (
    CitationVerificationError, SourceProof, covered_proofs, valid_text_range,
)


class QaSourceVerifier:
    def __init__(self, db):
        self.context_repository = QaContextRepository(db)

    async def verify(self, unit: ContextUnit, start: int, end: int) -> tuple[SourceProof, ...]:
        return (await self.verify_many([(unit, start, end)]))[0]

    async def verify_many(self, requests) -> list[tuple[SourceProof, ...]]:
        """Recheck all claims together, after generation, without a corpus reload."""
        if not requests:
            return []
        publications = {unit.publication_id for unit, _, _ in requests}
        if len(publications) != 1:
            raise CitationVerificationError("wrong_publication")
        for unit, start, end in requests:
            if not valid_text_range(unit.text, start, end):
                raise CitationVerificationError("quotation_range_invalid")
        chunk_ids = {unit.chunk_id for unit, _, _ in requests if unit.source_kind == "chunk"}
        provision_ids = {unit.provision_id for unit, _, _ in requests if unit.source_kind == "provision"}
        async with self.context_repository.proof_snapshot(next(iter(publications)),
                chunk_ids=chunk_ids, provision_ids=provision_ids) as catalog:
            for unit, _, _ in requests:
                item = catalog.items.get(unit.instrument_id)
                if (item is None or item.legal_version_id != unit.legal_version_id or
                    item.revision_id != unit.revision_id or item.chunk_set_id != unit.chunk_set_id):
                    raise CitationVerificationError("wrong_source_version")
                if unit.source_kind == "provision":
                    node = catalog.nodes.get(unit.provision_id)
                    if node is None or node.revision_id != item.revision_id or node.own_text != unit.text:
                        raise CitationVerificationError("source_text_changed")
                else:
                    chunk = catalog.chunks.get(unit.chunk_id)
                    if (chunk is None or chunk.chunk_set_id != item.chunk_set_id or
                        chunk.revision_id != item.revision_id or chunk.chunk_text != unit.text or
                        chunk.root_provision_id != unit.provision_id):
                        raise CitationVerificationError("source_text_changed")
            provision_refs = await catalog.provision_sources(provision_ids)
            rows = await self._chunk_rows(catalog, chunk_ids) if chunk_ids else []
            by_chunk = defaultdict(list)
            for row in rows:
                by_chunk[row[0].chunk_id].append(row)
            checked = {}
            result = []
            for unit, start, end in requests:
                key = (unit.source_kind, unit.chunk_id if unit.source_kind == "chunk" else unit.provision_id)
                if key not in checked:
                    if unit.source_kind == "provision":
                        node = catalog.nodes[unit.provision_id]
                        checked[key] = [SourceProof(
                            start=ref["provision_char_start"], end=ref["provision_char_end"],
                            provision_id=node.id, kind=node.kind, label=node.label,
                            reference_path=node.reference_path, source_span_id=ref["source_span_id"],
                            file_id=ref["file_id"], file_sha256=ref["file_sha256"],
                            filename=ref["filename"], page_index=ref["page_index"],
                            source_url=ref["source_url"],
                        ) for ref in provision_refs.get(node.id, [])]
                    else:
                        checked[key] = self._chunk_proofs(unit, by_chunk[unit.chunk_id])
                result.append(covered_proofs(unit.text, start, end, checked[key]))
            return result

    @staticmethod
    async def _chunk_rows(catalog, chunk_ids):
        return (await catalog.session.execute(
                select(LegalChunkProvision, ProvisionSpan, Provision, FilePage,
                       FileBlock, FileExtraction, LegalFile, RevisionSource)
                .join(ProvisionSpan, (ProvisionSpan.id == LegalChunkProvision.source_span_id) &
                      (ProvisionSpan.provision_id == LegalChunkProvision.provision_id) &
                      (ProvisionSpan.revision_id == LegalChunkProvision.revision_id))
                .join(Provision, (Provision.id == LegalChunkProvision.provision_id) &
                      (Provision.revision_id == LegalChunkProvision.revision_id))
                .join(FilePage, (FilePage.id == ProvisionSpan.page_id) &
                      (FilePage.extraction_id == ProvisionSpan.extraction_id))
                .join(FileBlock, (FileBlock.id == ProvisionSpan.block_id) &
                      (FileBlock.page_id == FilePage.id))
                .join(FileExtraction, FileExtraction.id == ProvisionSpan.extraction_id)
                .join(LegalFile, LegalFile.id == FileExtraction.file_id)
                .join(RevisionSource, (RevisionSource.revision_id == ProvisionSpan.revision_id) &
                      (RevisionSource.extraction_id == ProvisionSpan.extraction_id) &
                      (RevisionSource.file_id == LegalFile.id) &
                      (RevisionSource.legal_version_id.in_(
                          [item.legal_version_id for item in catalog.items.values()])))
                .where(LegalChunkProvision.chunk_id.in_(chunk_ids),
                       LegalChunkProvision.revision_id.in_(
                           [item.revision_id for item in catalog.items.values()]))
                .order_by(LegalChunkProvision.chunk_id, LegalChunkProvision.ordinal)
            )).all()

    @staticmethod
    def _chunk_proofs(unit, rows):
        proofs = []
        for link, span, node, page, block, extraction, file, source in rows:
            if (span.mapping_quality != "exact" or
                not valid_text_range(unit.text, link.chunk_char_start, link.chunk_char_end) or
                not valid_text_range(node.own_text, link.provision_char_start, link.provision_char_end) or
                not valid_text_range(node.own_text, span.provision_char_start, span.provision_char_end) or
                not valid_text_range(block.normalized_text, span.block_char_start, span.block_char_end) or
                not (link.provision_char_start <= span.provision_char_start <
                     span.provision_char_end <= link.provision_char_end)):
                raise CitationVerificationError("source_mapping_not_exact")
            fragment = unit.text[link.chunk_char_start:link.chunk_char_end]
            if fragment != node.own_text[link.provision_char_start:link.provision_char_end]:
                raise CitationVerificationError("source_text_changed")
            before = node.own_text[link.provision_char_start:span.provision_char_start]
            after = node.own_text[span.provision_char_end:link.provision_char_end]
            if (before.strip() or after.strip() or
                node.own_text[span.provision_char_start:span.provision_char_end] !=
                block.normalized_text[span.block_char_start:span.block_char_end]):
                raise CitationVerificationError("source_text_changed")
            core_start = link.chunk_char_start + len(before)
            core_end = core_start + (span.provision_char_end - span.provision_char_start)
            proofs.append(SourceProof(
                start=core_start, end=core_end,
                provision_id=node.id, kind=node.kind, label=node.label,
                reference_path=node.reference_path, source_span_id=span.id,
                file_id=file.id, file_sha256=file.sha256,
                filename=file.original_filename, page_index=page.page_index,
                source_url=file.source_url,
            ))
        return proofs
