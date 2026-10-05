"""Loopback-only Act/Rules PDF registration; no extraction worker yet."""

from __future__ import annotations

import ipaddress
import re
from datetime import date
from urllib.parse import urlsplit
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status

from app.api.deps import LegalIngestionRepositoryDep, SettingsDep
from app.core.errors import (
    LegalUploadConflictError,
    LegalUploadStorageError,
    LegalUploadValidationError,
)
from app.repositories.legal_ingestion_repository import LegalRegistration
from app.schemas.legal_ingestion import LegalUploadOut
from app.services.legal_upload import store_legal_pdf

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")


def _loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_local_ingestion(request: Request, settings: SettingsDep) -> None:
    """Two explicit gates plus loopback checks; never trust CORS as security."""
    if not settings.legal_ingest_enabled or settings.environment.lower() != "development":
        raise HTTPException(status_code=404, detail="Not found")
    if request.client is None or not _loopback_host(request.client.host):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not _loopback_host(host):
        raise HTTPException(status_code=404, detail="Not found")
    origin = request.headers.get("origin")
    if origin:
        try:
            trusted_origin = origin in settings.cors_origins_list and _loopback_host(
                urlsplit(origin).hostname
            )
        except ValueError:
            trusted_origin = False
        if not trusted_origin:
            raise HTTPException(status_code=403, detail="Untrusted origin")


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
    file: Annotated[UploadFile, File(description="An original Act or Rules PDF")],
    jurisdiction_code: Annotated[str, Form(max_length=50)],
    jurisdiction_name: Annotated[str, Form(max_length=200)],
    category_slug: Annotated[str, Form(max_length=100)],
    category_name: Annotated[str, Form(max_length=200)],
    instrument_type: Annotated[Literal["act", "rule"], Form()],
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
async def recent_legal_uploads(repository: LegalIngestionRepositoryDep) -> list[LegalUploadOut]:
    return [LegalUploadOut.model_validate(record) for record in await repository.recent_uploads()]
