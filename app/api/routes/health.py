from fastapi import APIRouter

from app.api.deps import DatabaseDep
from app.core.config import get_settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(db: DatabaseDep) -> HealthResponse:
    """Always answers 200 while the app is running.

    `status` is "degraded" if the database cannot be reached. /ask still works
    in that case because it uses fixtures for now.
    """
    settings = get_settings()
    db_ok = await db.ping()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "unavailable",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )
