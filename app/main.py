"""LegalMind AI backend entry point.

Run locally:
    uvicorn app.main:app --reload
Docs (OpenAPI, outside production):
    http://127.0.0.1:8000/docs
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.middleware import RequestContextMiddleware
from app.api.routes import contracts, health, legal_ingestion, legal_inspection, legal_search, publications, qa
from app.bootstrap import build_services
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import Database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    # Connect to the database ONE time. Every request reuses this pool.
    db = Database(settings)
    app.state.db = db
    await db.connect()

    app.state.services = None
    if settings.legal_search_enabled:
        services = build_services(db, settings)
        app.state.services = services
        # Read the active publication into memory, then keep it current.
        await services.index.refresh()
        services.index.start()
        if settings.models_warm_on_startup:
            services.start_warmup()
        if not services.generator.configured:
            logger.warning("No answer-model API key is configured; /ask will return 503 until one is set.")
    if settings.is_production and not settings.api_key_set:
        logger.warning("API_KEYS is empty: /ask and /search/legal are open to any caller.")

    logger.info("%s v%s started (%s)", settings.app_name, settings.app_version, settings.environment)
    try:
        yield
    finally:
        if app.state.services is not None:
            await app.state.services.aclose()
        await db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    docs = settings.serve_docs

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Legal question answering over a reviewed, published corpus of statutes, "
                    "with legal-PDF ingestion and review tooling for local administration.",
        debug=settings.debug and not settings.is_production,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Server-Timing", "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(legal_search.router)
    app.include_router(qa.router)
    if settings.is_development:
        # Unauthenticated administration and contract uploads exist only for
        # loopback development; other environments do not mount them at all.
        app.include_router(contracts.router)
        app.include_router(legal_ingestion.router)
        app.include_router(legal_inspection.router)
        app.include_router(publications.router)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        route = request.scope.get("route")
        logger.error("request_failed method=%s route=%s error=%s", request.method,
                     getattr(route, "path", "unknown"), type(exc).__name__, exc_info=settings.debug)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
