"""Loopback-only legal-PDF registration and extraction inspection."""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from datetime import date
from uuid import UUID
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

import fitz

from app.api.deps import DatabaseDep, LegalIngestionRepositoryDep, SettingsDep, require_local_development
from app.core.errors import (
    LegalUploadConflictError,
    LegalUploadStorageError,
    LegalUploadValidationError,
)
from app.repositories.legal_ingestion_repository import LegalRegistration
from app.repositories.legal_extraction_repository import LegalExtractionRepository
from app.repositories.legal_structure_repository import LegalStructureRepository, StructureConflict
from app.schemas.legal_extraction import (
    FileOverviewOut,
    FilePageDetailOut,
    FilePageSummaryOut,
)
from app.schemas.legal_ingestion import LegalUploadOut
from app.schemas.legal_structure import ApprovalDecisionIn, StructureCreateIn
from app.services.legal_extraction import SourcePdfError, resolve_source_pdf
from app.services.legal_upload import store_legal_pdf

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")


def _require_local_ingestion(request: Request, settings: SettingsDep) -> None:
    """Two explicit gates plus loopback checks; never trust CORS as security."""
    if not settings.legal_ingest_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    require_local_development(request, settings)


router = APIRouter(
    prefix="/local-admin/legal",
    tags=["local legal ingestion"],
    dependencies=[Depends(_require_local_ingestion)],
    include_in_schema=False,
)


def _clean_metadata(
    jurisdiction_code: str, jurisdiction_name: str,
    category_slug: str, category_name: str,
    instrument_type: str, canonical_title: str,
    citation_key: str, edition_key: str, assignment_role: str,
    publication_date: date | None, effective_from: date | None,
    effective_to: date | None, date_source: str | None,
    publisher: str | None, source_url: str | None,
) -> LegalRegistration:
    jurisdiction_code = jurisdiction_code.strip().upper()
    jurisdiction_name = jurisdiction_name.strip()
    category_slug = category_slug.strip().lower()
    category_name = category_name.strip()
    canonical_title = canonical_title.strip()
    citation_key = citation_key.strip().lower()
    edition_key = edition_key.strip()
    date_source = date_source.strip() if date_source else None
    publisher = publisher.strip() if publisher else None
    source_url = source_url.strip() if source_url else None
    if not _CODE.fullmatch(jurisdiction_code):
        raise LegalUploadValidationError("Jurisdiction code must use letters, numbers, or hyphens.")
    if not _SLUG.fullmatch(category_slug) or not _SLUG.fullmatch(citation_key):
        raise LegalUploadValidationError("Category slug and citation key must use lowercase letters, numbers, or hyphens.")
    if not all((jurisdiction_name, category_name, canonical_title, edition_key)):
        raise LegalUploadValidationError("Law and edition fields cannot be blank.")
    if effective_from and effective_to and effective_to <= effective_from:
        raise LegalUploadValidationError("Effective-to date must be later than effective-from date.")
    if source_url:
        parsed = urlsplit(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LegalUploadValidationError("Source URL must be an HTTP or HTTPS address.")
    return LegalRegistration(
        jurisdiction_code=jurisdiction_code,
        jurisdiction_name=jurisdiction_name,
        category_slug=category_slug,
        category_name=category_name,
        instrument_type=instrument_type,
        canonical_title=canonical_title,
        citation_key=citation_key,
        edition_key=edition_key,
        assignment_role=assignment_role,
        publication_date=publication_date,
        effective_from=effective_from,
        effective_to=effective_to,
        date_source=date_source or None,
        publisher=publisher or None,
        source_url=source_url or None,
    )


@router.post("/uploads", response_model=LegalUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_legal_pdf(
    response: Response,
    settings: SettingsDep,
    repository: LegalIngestionRepositoryDep,
    file: Annotated[UploadFile, File(description="An original Act, Rules, or Ordinance PDF")],
    jurisdiction_code: Annotated[str, Form(max_length=50)],
    jurisdiction_name: Annotated[str, Form(max_length=200)],
    category_slug: Annotated[str, Form(max_length=100)],
    category_name: Annotated[str, Form(max_length=200)],
    instrument_type: Annotated[Literal["act", "rule", "ordinance"], Form()],
    canonical_title: Annotated[str, Form(max_length=500)],
    citation_key: Annotated[str, Form(max_length=160)],
    edition_key: Annotated[str, Form(max_length=160)],
    assignment_role: Annotated[Literal["primary", "supplement", "amendment"], Form()] = "primary",
    publication_date: Annotated[date | None, Form()] = None,
    effective_from: Annotated[date | None, Form()] = None,
    effective_to: Annotated[date | None, Form()] = None,
    date_source: Annotated[str | None, Form(max_length=1000)] = None,
    publisher: Annotated[str | None, Form(max_length=200)] = None,
    source_url: Annotated[str | None, Form(max_length=2000)] = None,
) -> LegalUploadOut:
    try:
        metadata = _clean_metadata(
            jurisdiction_code, jurisdiction_name, category_slug, category_name,
            instrument_type, canonical_title, citation_key, edition_key,
            assignment_role, publication_date, effective_from, effective_to,
            date_source, publisher, source_url,
        )
        stored = await store_legal_pdf(
            file, upload_dir=settings.legal_upload_dir,
            max_size_bytes=settings.legal_upload_max_size_bytes,
            max_pages=settings.legal_upload_max_pages,
        )
        record = await repository.register_upload(metadata, stored)
    except LegalUploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LegalUploadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LegalUploadStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if record.already_registered:
        response.status_code = status.HTTP_200_OK
    return LegalUploadOut.model_validate(record)


@router.get("/uploads", response_model=list[LegalUploadOut])
async def recent_legal_uploads(
    repository: LegalIngestionRepositoryDep,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[LegalUploadOut]:
    return [LegalUploadOut.model_validate(record)
            for record in await repository.recent_uploads(limit=limit, offset=offset)]


@router.get("/files/{file_id}/overview", response_model=FileOverviewOut)
async def legal_file_overview(file_id: UUID, db: DatabaseDep) -> FileOverviewOut:
    overview = await LegalExtractionRepository(db).overview(file_id)
    if overview is None:
        raise HTTPException(status_code=404, detail="Registered PDF not found")
    return FileOverviewOut.model_validate(overview)


@router.get("/extractions/{extraction_id}/pages", response_model=list[FilePageSummaryOut])
async def extraction_pages(extraction_id: UUID, db: DatabaseDep) -> list[FilePageSummaryOut]:
    pages = await LegalExtractionRepository(db).pages(extraction_id)
    if pages is None:
        raise HTTPException(status_code=404, detail="Extraction not found")
    return [FilePageSummaryOut.model_validate(page) for page in pages]


@router.get("/extractions/{extraction_id}/pages/{page_index}", response_model=FilePageDetailOut)
async def extraction_page_detail(
    extraction_id: UUID, page_index: int, db: DatabaseDep
) -> FilePageDetailOut:
    if page_index < 1:
        raise HTTPException(status_code=404, detail="Page not found")
    page = await LegalExtractionRepository(db).page_detail(extraction_id, page_index)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return FilePageDetailOut.model_validate(page)


@router.post("/extractions/{extraction_id}/pages/{page_index}/mark-blank")
async def mark_blank_page(
    extraction_id: UUID, page_index: int, db: DatabaseDep,
    reason: Annotated[str, Body(embed=True, min_length=5, max_length=2000)],
) -> dict[str, bool]:
    if page_index < 1:
        raise HTTPException(status_code=404, detail="Page not found")
    try:
        found = await LegalExtractionRepository(db).mark_blank(extraction_id, page_index, reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="Page not found")
    return {"recorded": True}


def _render_pdf_page(path, page_index: int) -> bytes:
    with fitz.open(path) as document:
        page = document.load_page(page_index - 1)
        zoom = min(1.5, 1400 / max(float(page.rect.width), float(page.rect.height)))
        return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")


@router.get("/files/{file_id}/pages/{page_index}/preview")
async def original_page_preview(
    file_id: UUID, page_index: int, db: DatabaseDep, settings: SettingsDep
) -> Response:
    source = await LegalExtractionRepository(db).get_file(file_id)
    if source is None or not 1 <= page_index <= source.page_count:
        raise HTTPException(status_code=404, detail="Page not found")
    try:
        path = resolve_source_pdf(settings.legal_upload_dir, source.object_key, source.sha256)
        png = await run_in_threadpool(_render_pdf_page, path, page_index)
    except (SourcePdfError, fitz.FileDataError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_extraction(job_id: UUID, db: DatabaseDep) -> dict[str, bool]:
    if not await LegalExtractionRepository(db).requeue(job_id):
        raise HTTPException(status_code=409, detail="Only a completed or failed extraction can be retried")
    return {"queued": True}


@router.get("/versions/{version_id}/structure-sources")
async def structure_source_options(version_id: UUID, db: DatabaseDep) -> list[dict]:
    return await LegalStructureRepository(db).source_options(version_id)


@router.post("/versions/{version_id}/revisions", status_code=201)
async def create_structure_revision(
    version_id: UUID, payload: StructureCreateIn, db: DatabaseDep
) -> dict:
    try:
        return await LegalStructureRepository(db).create_revision(version_id, payload.extraction_ids)
    except StructureConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/versions/{version_id}/revisions")
async def list_structure_revisions(version_id: UUID, db: DatabaseDep) -> list[dict]:
    return await LegalStructureRepository(db).list_revisions(version_id)


@router.get("/revisions/{revision_id}")
async def structure_revision_overview(revision_id: UUID, db: DatabaseDep) -> dict:
    result = await LegalStructureRepository(db).overview(revision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Draft revision not found")
    return result


@router.get("/revisions/{revision_id}/relationships")
async def proposed_instrument_relationships(revision_id: UUID, db: DatabaseDep) -> dict:
    result = await LegalStructureRepository(db).relationship_candidates(revision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Draft revision not found")
    return result


@router.get("/revisions/{revision_id}/provisions/{provision_id}")
async def structure_provision_detail(
    revision_id: UUID, provision_id: UUID, db: DatabaseDep
) -> dict:
    result = await LegalStructureRepository(db).provision_detail(revision_id, provision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Provision not found in this revision")
    return result


@router.get("/revisions/{revision_id}/coverage")
async def structure_block_coverage(
    revision_id: UUID, db: DatabaseDep,
    status_filter: Literal["mapped", "partial", "ignored", "unmapped"] | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
) -> list[dict]:
    result = await LegalStructureRepository(db).coverage(
        revision_id, status=status_filter, limit=limit, offset=offset
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Draft revision not found")
    return result


@router.get("/revisions/{revision_id}/audit")
async def structure_revision_audit(revision_id: UUID, db: DatabaseDep) -> dict:
    result = await LegalStructureRepository(db).audit(revision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Draft revision not found")
    return result


@router.get("/revisions/{revision_id}/approval-check")
async def structure_approval_check(revision_id: UUID, db: DatabaseDep) -> dict:
    result = await LegalStructureRepository(db).approval_check(revision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return result


@router.post("/revisions/{revision_id}/approve")
async def approve_structure_revision(
    revision_id: UUID, payload: ApprovalDecisionIn, db: DatabaseDep
) -> dict:
    try:
        result = await LegalStructureRepository(db).approve(
            revision_id, expected_content_sha256=payload.expected_content_sha256,
            reason=payload.reason, acknowledge_findings=payload.acknowledge_findings,
        )
    except StructureConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return result


@router.get("/revisions/{revision_id}/extractions/{extraction_id}/pages/{page_index}/audit")
async def structure_page_audit(
    revision_id: UUID, extraction_id: UUID, page_index: int, db: DatabaseDep,
) -> dict:
    if page_index < 1:
        raise HTTPException(status_code=404, detail="Page not found in this revision")
    result = await LegalStructureRepository(db).page_audit(revision_id, extraction_id, page_index)
    if result is None:
        raise HTTPException(status_code=404, detail="Page not found in this revision")
    return result


@router.post("/revisions/{revision_id}/retry", status_code=202)
async def retry_structure_revision(revision_id: UUID, db: DatabaseDep) -> dict[str, bool]:
    if not await LegalStructureRepository(db).retry(revision_id):
        raise HTTPException(status_code=409, detail="Only an unparsed failed draft can be retried")
    return {"queued": True}
