"""Shared FastAPI dependencies.

The database pool and the retrieval/answer services live on `app.state`.
Repositories use a session per request.
"""

from collections.abc import AsyncIterator
from hmac import compare_digest
from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.ratelimit import RateLimiter
from app.db.session import Database
from app.repositories.document_repository import DocumentRepository
from app.repositories.legal_ingestion_repository import LegalIngestionRepository

_limiter = RateLimiter()


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
    if not settings.is_development or not request.client or not _loopback(request.client.host):
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


def _presented_key(request: Request) -> str:
    key = request.headers.get("x-api-key", "")
    if not key:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        key = value.strip() if scheme.casefold() == "bearer" else ""
    return key


def require_api_key(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """Enforce API_KEYS when configured; public endpoints stay open otherwise."""
    expected = settings.api_key_set
    if not expected:
        return
    presented = _presented_key(request)
    if not presented or not any(compare_digest(presented, key) for key in expected):
        raise HTTPException(status_code=401, detail="A valid API key is required.",
                            headers={"WWW-Authenticate": "Bearer"})


def enforce_rate_limit(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """Per-client ceiling for the public question and search endpoints."""
    client = request.client.host if request.client else "unknown"
    allowed, retry_after = _limiter.allow(f"{_presented_key(request)[:16]}|{client}",
                                          settings.rate_limit_per_minute)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.",
                            headers={"Retry-After": str(max(1, round(retry_after)))})


def get_services(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
    """Retrieval and answer services; 404 while search is disabled."""
    if not settings.legal_search_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="The search service is not ready.")
    return services


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
DocumentRepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]
LegalIngestionRepositoryDep = Annotated[LegalIngestionRepository, Depends(get_legal_ingestion_repository)]
