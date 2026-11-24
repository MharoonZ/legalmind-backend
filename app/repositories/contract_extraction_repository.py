"""Transactional persistence for uploaded-contract extraction results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ContractExtractionError
from app.db.models import (
    ContractClause,
    ContractClauseSpan,
    ContractPage,
    Document,
    DocumentVersion,
)
from app.services.contract_citations import ContractClauseEvidenceSource
from app.services.contract_extraction import (
    ClausePageSpan,
    ExtractedContract,
    ExtractionSource,
)

# A successful version is immutable because summaries may already reference its
# clause IDs. A corrected/reprocessed contract must use a new DocumentVersion.
_EXTRACTABLE_STATUSES = {"uploaded", "extraction_failed"}
# A run killed mid-way leaves the version "extracting"; after this long another
# run may take it over instead of the contract being stuck for good.
_STALE_EXTRACTION = timedelta(minutes=30)


class ContractExtractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_extraction_source(self, version_id: str) -> ExtractionSource | None:
        statement = (
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.id == version_id)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        document, version = row
        return ExtractionSource(
            document_id=document.id,
            version_id=version.id,
            storage_path=document.storage_path,
            pdf_sha256=document.pdf_sha256,
            page_count=document.page_count,
            status=version.status,
        )

    async def mark_extracting(self, version_id: str) -> None:
        statement = (
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .with_for_update()
        )
        version = (await self.session.execute(statement)).scalar_one_or_none()
        if version is None:
            raise ContractExtractionError("Document version was not found.")
        started = version.extraction_started_at
        abandoned = (version.status == "extracting" and started is not None
                     and datetime.now(UTC) - started > _STALE_EXTRACTION)
        if version.status not in _EXTRACTABLE_STATUSES and not abandoned:
            raise ContractExtractionError(
                f"Document version cannot be extracted while status is '{version.status}'."
            )
        version.status = "extracting"
        version.error_message = None
        version.extraction_engine = None
        version.extraction_engine_version = None
        version.extracted_text_sha256 = None
        version.extraction_started_at = datetime.now(UTC)
        version.extraction_completed_at = None
        await self.session.commit()

    async def replace_extraction(self, extraction: ExtractedContract) -> None:
        try:
            statement = (
                select(DocumentVersion)
                .where(DocumentVersion.id == extraction.document_version_id)
                .with_for_update()
            )
            version = (await self.session.execute(statement)).scalar_one_or_none()
            if version is None:
                raise ContractExtractionError("Document version was not found.")
            if version.status != "extracting":
                raise ContractExtractionError(
                    "Extraction results can only be saved while status is 'extracting'."
                )

            # Replace as one transaction. Existing successful data remains
            # available if conversion fails before this method is called.
            await self.session.execute(
                delete(ContractClause).where(
                    ContractClause.document_version_id == extraction.document_version_id
                )
            )
            await self.session.execute(
                delete(ContractPage).where(
                    ContractPage.document_version_id == extraction.document_version_id
                )
            )

            self.session.add_all(
                [
                    ContractPage(
                        document_version_id=extraction.document_version_id,
                        page_number=page.page_number,
                        text=page.text,
                        text_sha256=page.text_sha256,
                    )
                    for page in extraction.pages
                ]
            )
            for extracted_clause in extraction.clauses:
                clause = ContractClause(
                    id=extracted_clause.clause_id,
                    document_version_id=extraction.document_version_id,
                    ordinal=extracted_clause.ordinal,
                    label=extracted_clause.label,
                    heading=extracted_clause.heading,
                    text=extracted_clause.text,
                    text_sha256=extracted_clause.text_sha256,
                    page_start=extracted_clause.page_start,
                    page_end=extracted_clause.page_end,
                )
                clause.spans = [
                    ContractClauseSpan(
                        document_version_id=extraction.document_version_id,
                        page_number=span.page_number,
                        clause_char_start=span.clause_char_start,
                        clause_char_end=span.clause_char_end,
                    )
                    for span in extracted_clause.spans
                ]
                self.session.add(clause)

            version.status = "extracted"
            version.error_message = None
            version.extraction_engine = extraction.engine_name
            version.extraction_engine_version = extraction.engine_version
            version.extracted_text_sha256 = extraction.extracted_text_sha256
            version.extraction_completed_at = datetime.now(UTC)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def mark_extraction_failed(self, version_id: str, message: str) -> None:
        try:
            statement = (
                select(DocumentVersion)
                .where(DocumentVersion.id == version_id)
                .with_for_update()
            )
            version = (await self.session.execute(statement)).scalar_one_or_none()
            if version is None:
                return
            version.status = "extraction_failed"
            version.error_message = message[:2000]
            version.extraction_completed_at = None
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def get_clause_sources(
        self, *, document_id: str, version_id: str
    ) -> dict[str, ContractClauseEvidenceSource]:
        """Load only clauses that provably belong to this document version."""
        belongs = (
            await self.session.execute(
                select(DocumentVersion.id).where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.status.in_(("extracted", "summarizing", "summarized")),
                )
            )
        ).scalar_one_or_none()
        if belongs is None:
            return {}

        clauses = (
            (
                await self.session.execute(
                    select(ContractClause)
                    .where(ContractClause.document_version_id == version_id)
                    .order_by(ContractClause.ordinal)
                )
            )
            .scalars()
            .all()
        )
        span_rows = (
            (
                await self.session.execute(
                    select(ContractClauseSpan)
                    .where(ContractClauseSpan.document_version_id == version_id)
                    .order_by(
                        ContractClauseSpan.clause_id,
                        ContractClauseSpan.clause_char_start,
                    )
                )
            )
            .scalars()
            .all()
        )
        spans_by_clause: dict[str, list[ClausePageSpan]] = {}
        for span in span_rows:
            spans_by_clause.setdefault(span.clause_id, []).append(
                ClausePageSpan(
                    page_number=span.page_number,
                    clause_char_start=span.clause_char_start,
                    clause_char_end=span.clause_char_end,
                )
            )

        return {
            clause.id: ContractClauseEvidenceSource(
                document_id=document_id,
                version_id=version_id,
                clause_id=clause.id,
                text=clause.text,
                spans=tuple(spans_by_clause.get(clause.id, ())),
            )
            for clause in clauses
        }
