"""Shared FastAPI dependencies.

The database pool lives on `app.state`. Repositories use a session per request.
"""

from collections.abc import AsyncIterator
from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
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


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def require_local_development(request: Request, settings: Settings) -> None:
    if settings.environment.casefold() != "development" or not request.client or not _loopback(request.client.host):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not _loopback(host):
        raise HTTPException(status_code=404, detail="Not found")
    origin = request.headers.get("origin")
    if origin:
        try:
            allowed = origin in settings.cors_origins_list and _loopback(urlsplit(origin).hostname)
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(status_code=403, detail="Untrusted origin")


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
DocumentRepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]
LegalIngestionRepositoryDep = Annotated[LegalIngestionRepository, Depends(get_legal_ingestion_repository)]
