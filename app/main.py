"""LegalMind AI backend entry point.

Run locally:
    uvicorn app.main:app --reload
Docs (OpenAPI):
    http://127.0.0.1:8000/docs
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import contracts, health, legal_ingestion, legal_search, publications
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import Database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Connect to the database ONE time. Every request reuses this pool.
    db = Database(settings)
    app.state.db = db
    await db.connect()

    logger.info(
        "%s v%s started (%s)", settings.app_name, settings.app_version, settings.environment
    )
    yield

    await db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Legal PDF ingestion, validated corpus publications, legal evidence retrieval, and private contract APIs.",
        debug=settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(contracts.router)
    app.include_router(legal_ingestion.router)
    app.include_router(publications.router)
    app.include_router(legal_search.router)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
