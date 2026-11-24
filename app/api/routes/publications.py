"""Publication administration shares the existing loopback development gate."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import DatabaseDep
from app.api.routes.legal_ingestion import _require_local_ingestion
from app.repositories.publication_repository import PublicationRepository
from app.schemas.publications import PublicationActivateIn, PublicationCreateIn
from app.services.corpus_validation import PublicationValidationError

router = APIRouter(prefix="/local-admin/legal/publications", tags=["local publications"],
    dependencies=[Depends(_require_local_ingestion)], include_in_schema=False)


@router.get("")
async def list_publications(db: DatabaseDep):
    return await PublicationRepository(db).list()


@router.post("/validate")
async def validate_publication(payload: PublicationCreateIn, db: DatabaseDep):
    try:
        return await PublicationRepository(db).validate(payload.chunk_set_ids)
    except PublicationValidationError as exc:
        raise HTTPException(409, detail={"issues": exc.issues}) from exc


@router.post("", status_code=201)
async def create_publication(payload: PublicationCreateIn, db: DatabaseDep):
    try:
        return await PublicationRepository(db).create(payload.name, payload.chunk_set_ids, notes=payload.notes)
    except PublicationValidationError as exc:
        raise HTTPException(409, detail={"issues": exc.issues}) from exc


@router.post("/{publication_id}/activate")
async def activate_publication(publication_id: UUID, payload: PublicationActivateIn, db: DatabaseDep,
                               request: Request):
    try:
        result = await PublicationRepository(db).activate(publication_id,
            expected_manifest_sha256=payload.expected_manifest_sha256)
    except LookupError as exc:
        raise HTTPException(404, detail="Publication not found") from exc
    except PublicationValidationError as exc:
        raise HTTPException(409, detail={"issues": exc.issues}) from exc
    # Serve the newly activated publication at once instead of on the next poll.
    services = getattr(request.app.state, "services", None)
    if services is not None:
        await services.index.refresh()
    return result
