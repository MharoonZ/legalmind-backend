"""Local-only document inspection; all endpoints are read-only."""
import base64
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import DbSessionDep, SettingsDep
from app.api.routes.legal_ingestion import _require_local_ingestion
from app.repositories.legal_inspection_repository import LegalInspectionRepository
from app.services.legal_pdf_extraction import PdfProblem
from app.services.pdf_process import pdf_process


def get_inspection_repository(session: DbSessionDep):
    return LegalInspectionRepository(session)


InspectorDep = Annotated[LegalInspectionRepository, Depends(get_inspection_repository)]
router = APIRouter(prefix="/local-admin/legal", tags=["local legal inspection"],
                   dependencies=[Depends(_require_local_ingestion)], include_in_schema=False)


def found(value):
    if value is None:
        raise HTTPException(404, "Record not found")
    return value


@router.get("/documents")
async def documents(repository: InspectorDep, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await repository.documents(limit=limit, offset=offset)


@router.get("/documents/{file_id}")
async def document(file_id: UUID, repository: InspectorDep):
    return found(await repository.document(file_id))


@router.get("/jobs/{job_id}")
async def job(job_id: UUID, repository: InspectorDep):
    return found(await repository.job(job_id))


@router.get("/extractions/{extraction_id}")
async def extraction(extraction_id: UUID, repository: InspectorDep):
    return found(await repository.extraction(extraction_id))


@router.get("/extractions/{extraction_id}/pages/{page_index}")
async def page(extraction_id: UUID, page_index: int, repository: InspectorDep):
    if page_index < 1:
        raise HTTPException(422, "Page numbers start at one")
    return found(await repository.page(extraction_id, page_index))


@router.get("/documents/{file_id}/pages/{page_index}/preview")
async def preview(file_id: UUID, page_index: int, repository: InspectorDep, settings: SettingsDep):
    source = found(await repository.source(file_id))
    if page_index < 1:
        raise HTTPException(422, "Page numbers start at one")
    source["root"] = settings.legal_upload_dir
    try:
        result = await pdf_process({"operation": "preview", "source": source,
            "page_index": page_index, "dpi": settings.legal_preview_dpi}, settings.legal_page_timeout)
    except PdfProblem as exc:
        code = str(exc)
        raise HTTPException(404 if code in {"source_pdf_missing", "page_out_of_range"} else 409,
                            {"code": code, "message": "The original page preview is unavailable. Check the registered PDF and its integrity."}) from exc
    return Response(base64.b64decode(result["png"]), media_type="image/png", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
