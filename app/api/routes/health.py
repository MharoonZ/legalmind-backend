from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import DatabaseDep, SettingsDep
from app.schemas.health import HealthResponse, RetrievalHealth

router = APIRouter(tags=["health"])


def _retrieval(request: Request, settings) -> RetrievalHealth:
    services = getattr(request.app.state, "services", None)
    if not settings.legal_search_enabled or services is None:
        return RetrievalHealth(enabled=settings.legal_search_enabled)
    state = services.index.status()
    return RetrievalHealth(
        enabled=True, publication_loaded=state["loaded"], publication_id=state.get("publication_id"),
        publication_name=state.get("name"), laws=state.get("laws"), chunks=state.get("chunks"),
        models_ready=services.models_ready, answer_model_configured=services.generator.configured)


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(request: Request, db: DatabaseDep, settings: SettingsDep) -> HealthResponse:
    """Always answers 200 while the app is running.

    `status` is "degraded" when the database cannot be reached.
    """
    db_ok = await db.ping()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "unavailable",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        retrieval=_retrieval(request, settings),
    )


@router.get("/health/live", summary="Liveness probe", include_in_schema=False)
async def live() -> dict[str, str]:
    """The process is up; no dependency is contacted."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe", include_in_schema=False)
async def ready(request: Request, settings: SettingsDep):
    """200 once this instance can serve: a publication is in memory when search is on."""
    retrieval = _retrieval(request, settings)
    is_ready = not settings.legal_search_enabled or retrieval.publication_loaded
    return JSONResponse(status_code=200 if is_ready else 503,
                        content={"status": "ready" if is_ready else "starting",
                                 "retrieval": retrieval.model_dump()})
