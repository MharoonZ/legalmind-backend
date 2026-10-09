"""Shared FastAPI dependencies.

The database pool lives on `app.state`. Repositories use a session per request.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import Database
from app.repositories.document_repository import DocumentRepository
from app.repositories.legal_ingestion_repository import LegalIngestionRepository


def get_database(request: Request) -> Database:
    """The one Database created at startup (see app.main lifespan)."""
    return request.app.state.db


async def get_db_session(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """A session that borrows a connection from the shared pool."""
    async with db.session() as session:
        yield session


def get_document_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentRepository:
    return DocumentRepository(session)


def get_legal_ingestion_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> LegalIngestionRepository:
    return LegalIngestionRepository(session)


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
DocumentRepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]
LegalIngestionRepositoryDep = Annotated[LegalIngestionRepository, Depends(get_legal_ingestion_repository)]
