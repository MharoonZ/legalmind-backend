"""Database access for uploaded contracts, separate from legal-corpus PDFs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentVersion


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Everything the API response needs, already flattened."""

    document_id: str
    version_id: str
    version_number: int
    status: str
    filename: str
    content_type: str
    size_bytes: int
    page_count: int
    question: str | None
    error_message: str | None
    created_at: datetime


def _to_record(document: Document, version: DocumentVersion) -> DocumentRecord:
    return DocumentRecord(
        document_id=document.id,
        version_id=version.id,
        version_number=version.version_number,
        status=version.status,
        filename=document.original_filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        page_count=document.page_count,
        question=version.question,
        error_message=version.error_message,
        created_at=version.created_at,
    )


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_upload(
        self,
        *,
        document_id: str,
        version_id: str,
        original_filename: str,
        content_type: str,
        pdf_sha256: str,
        size_bytes: int,
        page_count: int,
        storage_path: str,
        question: str | None,
    ) -> DocumentRecord:
        """Create the document + its first version (status="uploaded").

        Nothing here reads or writes clause/extraction data - see
        `app.db.models` for the statuses later pipeline stages will use.
        """
        document = Document(
            id=document_id,
            original_filename=original_filename,
            content_type=content_type,
            pdf_sha256=pdf_sha256,
            size_bytes=size_bytes,
            page_count=page_count,
            storage_path=storage_path,
        )
        version = DocumentVersion(
            id=version_id,
            document_id=document_id,
            version_number=1,
            status="uploaded",
            question=question,
        )
        self.session.add(document)
        self.session.add(version)
        await self.session.commit()
        return _to_record(document, version)

    async def get_latest(self, document_id: str) -> DocumentRecord | None:
        """The document plus its newest version, or None if unknown."""
        document = await self.session.get(Document, document_id)
        if document is None:
            return None
        statement = (
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc())
            .limit(1)
        )
        version = (await self.session.execute(statement)).scalar_one_or_none()
        if version is None:
            return None
        return _to_record(document, version)
