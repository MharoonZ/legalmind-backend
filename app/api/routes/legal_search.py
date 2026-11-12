import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.api.deps import DatabaseDep, SettingsDep
from app.repositories.legal_search_repository import LegalSearchRepository
from app.schemas.legal_search import LegalSearchIn, LegalSearchOut
from app.services.legal_retrieval import LegalRetrievalService

router = APIRouter(prefix="/search", tags=["legal retrieval"])


def get_legal_retrieval_service(db: DatabaseDep, settings: SettingsDep):
    if not settings.legal_search_enabled:
        raise HTTPException(404, detail="Not found")
    return LegalRetrievalService(LegalSearchRepository(db), settings)


@router.post("/legal", response_model=LegalSearchOut, responses={503: {"model": LegalSearchOut}})
async def search_legal(payload: LegalSearchIn, settings: SettingsDep, service=Depends(get_legal_retrieval_service)):
    try:
        async with asyncio.timeout(settings.legal_search_timeout_seconds):
            result = await service.search(payload)
    except TimeoutError:
        result = LegalSearchOut(status="service_unavailable", query=payload.query,
            retrieval_query=payload.query, message="The published evidence search timed out.")
    if result.status == "service_unavailable":
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result
