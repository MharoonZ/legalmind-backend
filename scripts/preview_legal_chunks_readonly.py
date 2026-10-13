"""Read-only v2 chunk preview for every approved revision (no inserts)."""

from __future__ import annotations

import asyncio
import json
from collections import Counter

from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import (
    CorpusRevision, FileBlock, FilePage, LegalInstrument, LegalVersion,
    Provision, ProvisionSpan,
)
from app.db.session import Database
from app.services.legal_chunking import (
    ProvisionNode, ProvisionSourceSpan, build_chunks_from_spans,
)


async def main() -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable")
        async with db.session() as session:
            revisions = (await session.execute(
                select(CorpusRevision, LegalInstrument.canonical_title)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .where(CorpusRevision.review_state == "approved")
                .order_by(LegalInstrument.canonical_title)
            )).all()
            results = []
            for revision, title in revisions:
                provision_rows = (await session.execute(
                    select(Provision).where(Provision.revision_id == revision.id)
                )).scalars().all()
                nodes = [ProvisionNode(
                    id=p.id, parent_id=p.parent_id, kind=p.kind, label=p.label,
                    reference_path=p.reference_path, ordinal=p.ordinal,
                    heading=p.heading, own_text=p.own_text, source_status=p.source_status,
                ) for p in provision_rows]
                spans = (await session.execute(
                    select(ProvisionSpan, FilePage.page_index, FileBlock.block_ordinal)
                    .join(FilePage, FilePage.id == ProvisionSpan.page_id)
                    .outerjoin(FileBlock, FileBlock.id == ProvisionSpan.block_id)
                    .where(ProvisionSpan.revision_id == revision.id)
                )).all()
                source = [ProvisionSourceSpan(
                    id=s.id, provision_id=s.provision_id, extraction_id=s.extraction_id,
                    page_index=page, block_ordinal=block or 0,
                    block_char_start=s.block_char_start or 0,
                    provision_char_start=s.provision_char_start,
                    provision_char_end=s.provision_char_end,
                    mapping_quality=s.mapping_quality,
                ) for s, page, block in spans]
                try:
                    built = build_chunks_from_spans(title, nodes, source)
                    results.append({"title": title, "revision_id": str(revision.id),
                                    "chunks": len(built.chunks),
                                    "fragments": built.report["source_fragments"],
                                    "warnings": built.report["warnings"][:5]})
                except ValueError as exc:
                    results.append({"title": title, "revision_id": str(revision.id),
                                    "error": str(exc)})
            print(json.dumps({"revisions": len(results),
                              "result_counts": dict(Counter("error" if "error" in r else "ok" for r in results)),
                              "results": results}, indent=2))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
