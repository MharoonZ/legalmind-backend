"""Read-only audit of the latest saved draft for every legal edition.

Emits one JSON line per edition so the corpus report can be reproduced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import fitz
from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import (
    CorpusRevision, LegalFile, LegalInstrument, LegalVersion, Provision, RevisionSource,
)
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository
from app.services.legal_structure import PARSER_VERSION, parse_structure
from app.services.legal_audit import structure_findings


async def main() -> None:
    settings = get_settings()
    db = Database(settings)
    if not await db.connect():
        raise RuntimeError("Database unavailable")
    try:
        repo = LegalStructureRepository(db)
        async with db.session() as session:
            revisions = (await session.execute(
                select(CorpusRevision.id, CorpusRevision.legal_version_id,
                       CorpusRevision.revision_number, LegalVersion.edition_key,
                       LegalInstrument.canonical_title)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .order_by(CorpusRevision.legal_version_id,
                          CorpusRevision.revision_number.desc())
            )).all()
        latest = {}
        for row in revisions:
            latest.setdefault(row.legal_version_id, row)
        for row in sorted(latest.values(), key=lambda item: item.canonical_title):
            overview = await repo.overview(row.id)
            async with db.session() as session:
                saved_nodes = (await session.scalars(select(Provision).where(
                    Provision.revision_id == row.id))).all()
            audit = await repo.audit(row.id)
            documents = await repo.load_sources(row.id)
            preview = parse_structure(documents)
            blocks = {block.id: block for document in documents for page in document.pages
                      for block in page.blocks}
            preview_spans = []
            for span in preview.spans:
                span_row = SimpleNamespace(**{field: getattr(span, field)
                                              for field in span.__dataclass_fields__})
                span_row.mapping_quality = "exact"
                preview_spans.append((span_row, SimpleNamespace(
                    normalized_text=blocks[span.block_id].text,
                    block_kind=blocks[span.block_id].kind,
                    bbox=blocks[span.block_id].bbox,
                )))
            preview_blocks = [SimpleNamespace(
                id=block.id, page_id=block.page_id, extraction_id=block.extraction_id,
                page_index=block.page_index, block_kind=block.kind,
                normalized_text=block.text, bbox=block.bbox,
            ) for block in blocks.values()]
            preview_findings = structure_findings(
                preview.nodes, preview_spans, source_blocks=preview_blocks,
                instrument_type=documents[0].instrument_type,
            )
            saved_paths = [item["reference_path"] for item in overview["provisions"]]
            preview_paths = [item.reference_path for item in preview.nodes]
            saved_by_path = {item.reference_path: item for item in saved_nodes}
            preview_by_path = {item.reference_path: item for item in preview.nodes}
            changed_text_paths = sorted(
                path for path in set(saved_by_path) & set(preview_by_path)
                if saved_by_path[path].own_text != preview_by_path[path].own_text
            )
            saved_statuses = [(item["reference_path"], item["source_status"])
                              for item in overview["provisions"]]
            preview_statuses = [(item.reference_path, item.source_status)
                                for item in preview.nodes]
            preview_span_counts = Counter(span.provision_id for span in preview.spans)
            saved_span_counts = [item["span_count"] for item in overview["provisions"]]
            expected_span_counts = [preview_span_counts[node.id] for node in preview.nodes]
            async with db.session() as session:
                files = (await session.execute(
                    select(LegalFile)
                    .join(RevisionSource, RevisionSource.file_id == LegalFile.id)
                    .where(RevisionSource.revision_id == row.id)
                )).scalars().all()
            pdfs = []
            for file in files:
                path = Path(settings.legal_upload_dir) / file.object_key
                raw = path.read_bytes()
                with fitz.open(stream=raw, filetype="pdf") as pdf:
                    actual_pages = len(pdf)
                pdfs.append({
                    "filename": file.original_filename,
                    "pages": file.page_count,
                    "actual_pages": actual_pages,
                    "hash_ok": hashlib.sha256(raw).hexdigest() == file.sha256,
                })
            findings = Counter(item["code"] for item in audit["findings"])
            statuses = [
                {"path": item["reference_path"], "status": item["source_status"]}
                for item in overview["provisions"] if item["source_status"]
            ]
            result = {
                "title": row.canonical_title,
                "edition": row.edition_key,
                "revision": str(row.id),
                "revision_number": row.revision_number,
                "parser_version": overview["parser_version"],
                "review_state": overview["review_state"],
                "job_status": overview["job"]["status"] if overview["job"] else None,
                "pdfs": pdfs,
                "saved_nodes": len(saved_paths),
                "preview_nodes": len(preview_paths),
                "added_paths_in_preview": sorted(set(preview_paths) - set(saved_paths)),
                "removed_paths_in_preview": sorted(set(saved_paths) - set(preview_paths)),
                "saved_sections": sum(item["kind"] == "section" for item in overview["provisions"]),
                "saved_subsections": sum(item["kind"] == "subsection" for item in overview["provisions"]),
                "preview_sections": sum(item.kind == "section" for item in preview.nodes),
                "preview_subsections": sum(item.kind == "subsection" for item in preview.nodes),
                "changed_text_paths_in_preview": changed_text_paths,
                "changed_text_examples": [
                    {"path": path,
                     "saved": saved_by_path[path].own_text[:200],
                     "preview": preview_by_path[path].own_text[:200]}
                    for path in changed_text_paths[:8]
                ],
                "saved_paths_match_preview": saved_paths == preview_paths if overview["parser_version"] == PARSER_VERSION else None,
                "saved_statuses_match_preview": saved_statuses == preview_statuses if overview["parser_version"] == PARSER_VERSION else None,
                "saved_span_counts_match_preview": saved_span_counts == expected_span_counts if overview["parser_version"] == PARSER_VERSION else None,
                "statuses": statuses,
                "coverage": audit["coverage_counts"],
                "findings": dict(findings),
                "preview_warnings": dict(Counter(item["code"] for item in preview.warnings)),
                "preview_findings": dict(Counter(item["code"] for item in preview_findings)),
            }
            print(json.dumps(result, default=str, ensure_ascii=False), flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
