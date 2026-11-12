"""POST/GET /contracts - upload a contract PDF and check its status.

Sprint 2, Abdullah's Week 1 scope: validate the file, save it, create the
document + document_version records. Nothing here reads clause text or
produces a summary - see LegalMind_Sprint2_Work_Division for what Haroon's
extraction and the Week 2 AI step still need to do with `version_id`.

The old Sprint 1 `/ask` endpoint has been retired. Contract uploads remain
separate from the new legal-PDF corpus and future question-answering API.
"""

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.api.deps import DocumentRepositoryDep, SettingsDep, require_local_development
from app.core.errors import DocumentStorageError, DocumentValidationError
from app.repositories.document_repository import DocumentRecord
from app.schemas.documents import DocumentVersionOut
from app.services.document_upload import (
    new_document_id,
    new_version_id,
    save_pdf_to_disk,
    validate_pdf_bytes,
)

logger = logging.getLogger(__name__)

def require_local_contracts(request: Request, settings: SettingsDep) -> None:
    # Uploaded contracts have no account or ownership model yet.
    require_local_development(request, settings)


router = APIRouter(prefix="/contracts", tags=["contracts"], dependencies=[Depends(require_local_contracts)])


def _to_response(record: DocumentRecord, message: str) -> DocumentVersionOut:
    return DocumentVersionOut(
        document_id=record.document_id,
        version_id=record.version_id,
        version_number=record.version_number,
        status=record.status,
        filename=record.filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        page_count=record.page_count,
        question=record.question,
        error_message=record.error_message,
        created_at=record.created_at,
        message=message,
    )


@router.post(
    "",
    response_model=DocumentVersionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one contract PDF, optionally with a chat question",
    responses={
        422: {"description": "Not a PDF, too large, too many pages, encrypted, or scanned"},
        503: {"description": "The file could not be saved"},
    },
)
async def upload_contract(
    settings: SettingsDep,
    repository: DocumentRepositoryDep,
    file: UploadFile = File(
        ..., description="One English service agreement, PDF, max 10 pages / 10 MB."
    ),
    question: str | None = Form(
        None,
        max_length=4000,
        description=(
            "Optional question about this contract. Saved for later - it "
            "cannot be answered until extraction and the AI summary step exist."
        ),
    ),
) -> DocumentVersionOut:
    """Validate, hash, and save the PDF; create document + version rows.

    Extraction is deliberately not started here: this endpoint only proves
    the file is real, usable, and safely stored. `status` always comes back
    "uploaded".
    """
    data = await file.read(settings.upload_max_size_bytes + 1)

    try:
        validated = await run_in_threadpool(validate_pdf_bytes,
            data,
            filename=file.filename or "upload.pdf",
            content_type=file.content_type,
            max_size_bytes=settings.upload_max_size_bytes,
            max_pages=settings.upload_max_pages,
            min_text_chars=settings.upload_min_text_chars,
        )
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    document_id = new_document_id()
    version_id = new_version_id()

    try:
        storage_path = await run_in_threadpool(save_pdf_to_disk,
            data, upload_dir=settings.upload_dir, document_id=document_id
        )
    except DocumentStorageError as exc:
        logger.error("Upload storage failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not save the uploaded file. Please try again.",
        ) from exc

    try:
        record = await repository.create_upload(
            document_id=document_id,
            version_id=version_id,
            original_filename=file.filename or "upload.pdf",
            content_type=file.content_type or "application/pdf",
            pdf_sha256=validated.sha256,
            size_bytes=validated.size_bytes,
            page_count=validated.page_count,
            storage_path=storage_path,
            question=question,
        )
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # A connection loss during COMMIT can leave a valid row behind. Keep
        # the original bytes for reconciliation rather than deleting them.
        logger.warning("contract_registration_failed document_id=%s error=%s", document_id, type(exc).__name__)
        raise HTTPException(503, detail="Could not confirm the contract registration.") from None

    message = "Saved. Waiting for clause extraction."
    if question:
        message += " Your question will be answered once extraction and the summary step are ready."
    return _to_response(record, message)


@router.get(
    "/{document_id}",
    response_model=DocumentVersionOut,
    summary="Check a contract's upload/processing status",
    responses={404: {"description": "No document with this id"}},
)
async def get_contract(document_id: str, repository: DocumentRepositoryDep) -> DocumentVersionOut:
    try:
        record = await repository.get_latest(document_id)
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.warning("contract_status_failed error=%s", type(exc).__name__)
        raise HTTPException(503, detail="Contract status is temporarily unavailable.") from None
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        )
    return _to_response(record, f"Current status: {record.status}.")
