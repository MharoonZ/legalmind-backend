"""POST /search/legal: ranked evidence candidates without a generated answer."""
import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep, enforce_rate_limit, get_services, require_api_key
from app.schemas.legal_search import LegalSearchIn, LegalSearchOut

router = APIRouter(prefix="/search", tags=["legal retrieval"],
                   dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])


@router.post("/legal", response_model=LegalSearchOut, responses={503: {"model": LegalSearchOut}},
             summary="Search the published collection")
async def search_legal(payload: LegalSearchIn, settings: SettingsDep, services=Depends(get_services)):
    try:
        async with asyncio.timeout(settings.legal_search_timeout_seconds):
            result = await services.retrieval.search(payload)
    except TimeoutError:
        result = LegalSearchOut(status="service_unavailable", query=payload.query,
            retrieval_query=payload.query, message="The published evidence search timed out.")
    if result.status == "service_unavailable":
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result
