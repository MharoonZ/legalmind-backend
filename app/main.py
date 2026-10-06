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

from app.api.routes import ask, contracts, health, legal_ingestion, legal_inspection
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import Database
from app.services.embeddings import EmbeddingService
from app.services.llm.factory import build_llm_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Connect to the database ONE time. Every request reuses this pool.
    db = Database(settings)
    app.state.db = db
    await db.connect()

    # One embedding model and one HTTP client for the whole process. Loading
    # the model per request would cost seconds and gigabytes.
    embeddings = EmbeddingService(
        settings.embedding_model,
        dimension=settings.embedding_dim,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        enabled=settings.embeddings_enabled,
    )
    app.state.embeddings = embeddings
    if settings.embeddings_enabled and settings.embedding_warmup:
        await embeddings.warmup()

    app.state.llm = build_llm_client(settings)
    if settings.llm_configured:
        logger.info("LLM: %s (%s)", settings.llm_provider, settings.llm_model)
    else:
        logger.warning(
            "%s is not set; /ask will return 503 on real questions", settings.llm_api_key_name
        )

    logger.info(
        "%s v%s started (%s)", settings.app_name, settings.app_version, settings.environment
    )
    yield

    await app.state.llm.aclose()
    await db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Grounded Q&A over the Contract Act, 1872 (sections 1-30).",
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
    app.include_router(ask.router)
    app.include_router(contracts.router)
    app.include_router(legal_ingestion.router)
    app.include_router(legal_inspection.router)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
