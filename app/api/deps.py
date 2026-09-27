"""Shared FastAPI dependencies.

Long-lived objects (the connection pool, the embedding model, the HTTP client
for OpenAI) live on `app.state` and are created once in the lifespan.
Per-request objects (a DB session, the repository, the service) are built here.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import Database
from app.repositories.document_repository import DocumentRepository
from app.repositories.law_repository import LawRepository
from app.services.ask_service import AskService
from app.services.embeddings import EmbeddingService
from app.services.llm.base import LLMClient


def get_database(request: Request) -> Database:
    """The one Database created at startup (see app.main lifespan)."""
    return request.app.state.db


def get_embedding_service(request: Request) -> EmbeddingService:
    return request.app.state.embeddings


def get_llm_client(request: Request) -> LLMClient:
    return request.app.state.llm


async def get_db_session(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """A session that borrows a connection from the shared pool."""
    async with db.session() as session:
        yield session


def get_law_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LawRepository:
    return LawRepository(session, settings.review_statuses_list)


def get_ask_service(
    repository: Annotated[LawRepository, Depends(get_law_repository)],
    embeddings: Annotated[EmbeddingService, Depends(get_embedding_service)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AskService:
    return AskService(repository, embeddings, llm, settings)


def get_document_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentRepository:
    return DocumentRepository(session)


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
LawRepositoryDep = Annotated[LawRepository, Depends(get_law_repository)]
EmbeddingServiceDep = Annotated[EmbeddingService, Depends(get_embedding_service)]
AskServiceDep = Annotated[AskService, Depends(get_ask_service)]
DocumentRepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]
