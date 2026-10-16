from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import DatabaseDep, SettingsDep
from app.repositories.legal_search_repository import LegalSearchRepository
from app.schemas.legal_search import LegalSearchIn, LegalSearchOut
from app.services.legal_retrieval import LegalRetrievalService

router = APIRouter(prefix="/search", tags=["legal retrieval"])


def get_legal_retrieval_service(db: DatabaseDep, settings: SettingsDep):
    if not settings.legal_search_enabled:
        raise HTTPException(404, detail="Not found")
    return LegalRetrievalService(LegalSearchRepository(db), settings)


@router.post("/legal", response_model=LegalSearchOut)
async def search_legal(payload: LegalSearchIn, service=Depends(get_legal_retrieval_service)):
    return await service.search(payload)
