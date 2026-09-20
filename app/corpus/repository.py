"""Insert one validated corpus version in a single transaction."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.corpus.models import CorpusVersion
from app.db.models import LawPassage, LawSection, LawSource


class CorpusRepository:
    """Database boundary for immutable legal evidence.

    The caller controls the transaction.  We never overwrite existing text or
    mark a source approved during import; both would make an old evidence ID
    silently point to a different legal claim.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def import_version(self, corpus: CorpusVersion) -> bool:
        """Insert a version; return False if the exact version already exists."""
        corpus.validate()
        existing = await self.session.get(LawSource, corpus.source_id)
        if existing is not None:
            if (
                existing.pdf_sha256 != corpus.pdf_sha256
                or existing.corpus_sha256 != corpus.corpus_sha256
                or existing.source_url != corpus.source_url
                or existing.law_name != corpus.law_name
                or existing.jurisdiction != corpus.jurisdiction
            ):
                raise ValueError("Existing source ID has conflicting provenance")
            # Count and compare hashes, not only IDs: a direct database edit
            # must never make an old evidence ID mean different text.
            section_rows = (
                await self.session.execute(
                    select(LawSection.id, LawSection.text, LawSection.text_sha256).where(
                        LawSection.source_id == corpus.source_id
                    )
                )
            ).all()
            passage_rows = (
                await self.session.execute(
                    select(LawPassage.evidence_id, LawPassage.text, LawPassage.text_sha256)
                    .join(LawSection)
                    .where(LawSection.source_id == corpus.source_id)
                )
            ).all()
            expected_sections = {
                s.section_id: (s.text, s.text_sha256) for s in corpus.sections
            }
            expected_passages = {
                p.evidence_id: (p.text, p.text_sha256)
                for section in corpus.sections
                for p in section.passages
            }
            actual_sections = {
                section_id: (text, text_sha) for section_id, text, text_sha in section_rows
            }
            actual_passages = {
                evidence_id: (text, text_sha) for evidence_id, text, text_sha in passage_rows
            }
            if actual_sections != expected_sections or actual_passages != expected_passages:
                raise ValueError("Existing corpus version is incomplete or has changed text")
            return False

        source = LawSource(
            id=corpus.source_id,
            law_name=corpus.law_name,
            jurisdiction=corpus.jurisdiction,
            source_url=corpus.source_url,
            pdf_sha256=corpus.pdf_sha256,
            corpus_sha256=corpus.corpus_sha256,
            pdf_page_count=corpus.pdf_page_count,
            review_status="needs_review",
        )
        for section in corpus.sections:
            section_row = LawSection(
                id=section.section_id,
                section_number=section.section_number,
                title=section.title,
                text=section.text,
                text_sha256=section.text_sha256,
                pdf_page_start=section.pdf_page_start,
                pdf_page_end=section.pdf_page_end,
            )
            for passage in section.passages:
                section_row.passages.append(
                    LawPassage(
                        evidence_id=passage.evidence_id,
                        page_number=passage.page_number,
                        text=passage.text,
                        text_sha256=passage.text_sha256,
                        # Include the Act and section title in keyword search,
                        # while keeping the exact evidence text separate.
                        search_text=(
                            f"{corpus.law_name} Pakistan section "
                            f"{section.section_number} {section.title}\n{passage.text}"
                        ),
                    )
                )
            source.sections.append(section_row)
        self.session.add(source)
        return True
