"""Shared FastAPI dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.services.ask_service import AskService


def get_database(request: Request) -> Database:
    """The one Database created at startup (see app.main lifespan)."""
    return request.app.state.db


async def get_db_session(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Give a route a session that uses the shared pool.

    Not used by /ask yet. Week 2 will use it when the service reads from the DB.
    """
    async with db.session() as session:
        yield session


def get_ask_service() -> AskService:
    return AskService()


DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
AskServiceDep = Annotated[AskService, Depends(get_ask_service)]
