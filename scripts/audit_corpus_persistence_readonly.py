"""Read-only, database-discovered PDF/page/block/span inventory for corpus audits.

Re-extracts native text in memory from immutable stored PDFs. It does not create
jobs, revisions, review events, or corrected extraction records.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import fitz
from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import (
    CorpusRevision, FileBlock, FileExtraction, FilePage, LegalFile,
    LegalFileAssignment, LegalInstrument, LegalVersion, ProcessingJob,
    Provision, ProvisionSpan, RevisionSource, ReviewEvent, SourceNoteMarkerLink,
)
from app.db.session import Database
from app.services.legal_extraction import _build_blocks, resolve_source_pdf


async def main() -> None:
    settings = get_settings()
    db = Database(settings)
    if not await db.connect():
        raise RuntimeError("Database unavailable")
    try:
        async with db.session() as session:
            files = (await session.scalars(select(LegalFile))).all()
            assignments = (await session.scalars(select(LegalFileAssignment))).all()
            versions = {row.id: row for row in (await session.scalars(select(LegalVersion))).all()}
            instruments = {row.id: row for row in (await session.scalars(select(LegalInstrument))).all()}
            revisions = (await session.scalars(select(CorpusRevision))).all()
            sources = (await session.scalars(select(RevisionSource))).all()
            extractions = (await session.scalars(select(FileExtraction))).all()
            pages = (await session.scalars(select(FilePage))).all()
            blocks = (await session.scalars(select(FileBlock))).all()
            provisions = (await session.scalars(select(Provision))).all()
            spans = (await session.scalars(select(ProvisionSpan))).all()
            links = (await session.scalars(select(SourceNoteMarkerLink))).all()
            reviews = (await session.scalars(select(ReviewEvent))).all()
            jobs = (await session.scalars(select(ProcessingJob))).all()

        assignments_by_file = defaultdict(list)
        for row in assignments:
            assignments_by_file[row.file_id].append(row)
        revisions_by_version = defaultdict(list)
        for row in revisions:
            revisions_by_version[row.legal_version_id].append(row)
        sources_by_revision = defaultdict(list)
        for row in sources:
            sources_by_revision[row.revision_id].append(row)
        extractions_by_id = {row.id: row for row in extractions}
        pages_by_extraction = defaultdict(list)
        for row in pages:
            pages_by_extraction[row.extraction_id].append(row)
        blocks_by_page = defaultdict(list)
        blocks_by_id = {row.id: row for row in blocks}
        for row in blocks:
            blocks_by_page[row.page_id].append(row)
        nodes_by_revision = defaultdict(list)
        nodes_by_id = {row.id: row for row in provisions}
        for row in provisions:
            nodes_by_revision[row.revision_id].append(row)
        spans_by_revision = defaultdict(list)
        for row in spans:
            spans_by_revision[row.revision_id].append(row)
        links_by_revision = Counter(row.revision_id for row in links)
        reviews_by_page = defaultdict(list)
        for row in reviews:
            reviews_by_page[row.page_id].append(row)
        jobs_by_revision = defaultdict(list)
        for row in jobs:
            if row.target_revision_id is not None:
                jobs_by_revision[row.target_revision_id].append(row)

        for file in sorted(files, key=lambda row: row.original_filename.casefold()):
            path = resolve_source_pdf(settings.legal_upload_dir, file.object_key, file.sha256)
            raw = Path(path).read_bytes()
            linked = []
            for assignment in assignments_by_file[file.id]:
                candidates = sorted(revisions_by_version[assignment.legal_version_id],
                                    key=lambda row: row.revision_number, reverse=True)
                for revision in candidates:
                    chosen_source = next((source for source in sources_by_revision[revision.id]
                                          if source.file_id == file.id), None)
                    if chosen_source is None:
                        continue
                    prior = next((item for item in candidates[1:]
                                  if any(source.file_id == file.id
                                         for source in sources_by_revision[item.id])), None)
                    linked.append((revision, chosen_source, prior))
                    break
            result = {
                "filename": file.original_filename,
                "file_id": str(file.id),
                "object_key": file.object_key,
                "sha256": file.sha256,
                "stored_sha256_ok": hashlib.sha256(raw).hexdigest() == file.sha256,
                "registered_pages": file.page_count,
                "editions": [],
            }
            with fitz.open(stream=raw, filetype="pdf") as pdf:
                result["physical_pages"] = len(pdf)
                for revision, source, prior in linked:
                    extraction = extractions_by_id.get(source.extraction_id)
                    source_pages = sorted(pages_by_extraction[source.extraction_id],
                                          key=lambda row: row.page_index)
                    problems = []
                    native_checked = 0
                    page_warnings = Counter()
                    outcomes = Counter()
                    methods = Counter()
                    tables = 0
                    for page in source_pages:
                        outcomes[page.outcome] += 1
                        methods[page.extraction_method] += 1
                        page_warnings.update(item.get("code", "unknown")
                                             for item in (page.warnings or []))
                        tables += len(page.table_regions or [])
                        page_blocks = sorted(blocks_by_page[page.id],
                                             key=lambda row: row.block_ordinal)
                        if page.page_index < 1 or page.page_index > len(pdf):
                            problems.append({"page": page.page_index, "code": "out_of_range_page"})
                            continue
                        if page.extraction_method in {"native", "none"}:
                            items = pdf[page.page_index - 1].get_text(
                                "blocks", flags=fitz.TEXTFLAGS_BLOCKS | fitz.TEXT_PRESERVE_IMAGES,
                                sort=True,
                            )
                            rerun_raw, _, rerun_blocks = _build_blocks(items)
                            native_checked += 1
                            if rerun_raw != page.raw_text:
                                problems.append({"page": page.page_index, "code": "native_raw_text_differs"})
                            if (len(rerun_blocks) != len(page_blocks)
                                    or any(left.raw_text != right.raw_text
                                           for left, right in zip(rerun_blocks, page_blocks))):
                                problems.append({"page": page.page_index, "code": "native_raw_blocks_differ"})
                        for block in page_blocks:
                            if block.page_char_start is None:
                                continue
                            if (block.page_char_end is None
                                    or page.normalized_text[block.page_char_start:block.page_char_end]
                                    != block.normalized_text):
                                problems.append({"page": page.page_index,
                                                 "block": block.block_ordinal,
                                                 "code": "page_block_offset_mismatch"})
                    expected = list(range(1, len(pdf) + 1))
                    actual = [page.page_index for page in source_pages]
                    if actual != expected:
                        problems.append({"code": "missing_or_duplicate_physical_pages",
                                         "expected": expected, "actual": actual})
                    own_nodes = nodes_by_revision[revision.id]
                    own_spans = spans_by_revision[revision.id]
                    prior_nodes = nodes_by_revision[prior.id] if prior is not None else []
                    current_paths = {node.reference_path: node for node in own_nodes}
                    prior_paths = {node.reference_path: node for node in prior_nodes}
                    for span in own_spans:
                        node = nodes_by_id.get(span.provision_id)
                        block = blocks_by_id.get(span.block_id)
                        if (node is None or block is None or block.page_id != span.page_id
                                or span.mapping_quality != "exact"
                                or node.own_text[span.provision_char_start:span.provision_char_end]
                                != block.normalized_text[span.block_char_start:span.block_char_end]):
                            problems.append({"code": "provision_source_span_mismatch",
                                             "span_id": str(span.id)})
                    version = versions[revision.legal_version_id]
                    instrument = instruments[version.instrument_id]
                    result["editions"].append({
                        "title": instrument.canonical_title,
                        "edition": version.edition_key,
                        "latest_revision": str(revision.id),
                        "previous_revision": str(prior.id) if prior else None,
                        "previous_comparison": {
                            "previous_nodes": len(prior_nodes),
                            "added_paths": len(current_paths.keys() - prior_paths.keys()),
                            "removed_paths": len(prior_paths.keys() - current_paths.keys()),
                            "changed_text_paths": sum(
                                current_paths[path].own_text != prior_paths[path].own_text
                                for path in current_paths.keys() & prior_paths.keys()
                            ),
                            "changed_source_status_paths": sum(
                                current_paths[path].source_status != prior_paths[path].source_status
                                for path in current_paths.keys() & prior_paths.keys()
                            ),
                        } if prior else None,
                        "parser_version": revision.parser_version,
                        "review_state": revision.review_state,
                        "structure_job_statuses": [job.status for job in jobs_by_revision[revision.id]],
                        "extraction_id": str(source.extraction_id),
                        "extraction_status": extraction.processing_status if extraction else None,
                        "page_rows": len(source_pages),
                        "native_pages_reextracted": native_checked,
                        "page_outcomes": dict(outcomes),
                        "extraction_methods": dict(methods),
                        "page_warning_codes": dict(page_warnings),
                        "table_regions": tables,
                        "node_counts": dict(Counter(node.kind for node in own_nodes)),
                        "source_statuses": dict(Counter(node.source_status for node in own_nodes
                                                        if node.source_status)),
                        "source_note_marker_links": links_by_revision[revision.id],
                        "span_count": len(own_spans),
                        "problems": problems,
                    })
            if not linked:
                result["missing_latest_revision"] = True
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
