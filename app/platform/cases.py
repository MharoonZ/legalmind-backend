from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import or_, select

from app.platform.common import Offset, agreement, dump, other_party
from app.platform.models import Agreement, Attachment, Case, CaseUpdate, Conversation
from app.platform.schemas import CaseIn, CaseNote
from app.platform.security import DB, User, audit, notify

router = APIRouter(prefix="/cases", tags=["case management"])


async def owned(db, identifier, user):
    row = await db.scalar(select(Case).where(Case.id == str(identifier)).with_for_update())
    if not row:
        raise HTTPException(404, "Case not found.")
    item, thread = await agreement(db, row.agreement_id, user)
    return row, item, thread


@router.get("")
async def cases(user: User, db: DB, offset: Offset = 0):
    query = (
        select(Case)
        .join(Agreement)
        .join(Conversation)
        .where(or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id))
    )
    return [dump(c) for c in (await db.scalars(query.order_by(Case.created_at.desc()).offset(offset).limit(200))).all()]


@router.post("", status_code=201)
async def create(body: CaseIn, user: User, db: DB):
    item, thread = await agreement(db, body.agreement_id, user)
    if item.active_version is None:
        raise HTTPException(409, "A case requires an approved, signed agreement.")
    row = Case(
        agreement_id=item.id,
        title=body.title,
        description=body.description,
        important_date=body.important_date.isoformat() if body.important_date else None,
    )
    db.add(row)
    await db.flush()
    audit(db, user, "case_created", row.id)
    notify(db, other_party(thread, user), "A case was opened", f"/cases/{row.id}")
    return dump(row)


@router.get("/{identifier}")
async def detail(identifier: UUID, user: User, db: DB):
    row, item, thread = await owned(db, identifier, user)
    updates = (
        await db.scalars(select(CaseUpdate).where(CaseUpdate.case_id == row.id).order_by(CaseUpdate.created_at))
    ).all()
    attachments = (await db.scalars(select(Attachment).where(Attachment.case_id == row.id))).all()
    return {
        **dump(row),
        "user_id": thread.user_id,
        "wakeel_id": thread.wakeel_id,
        "updates": [dump(u) for u in updates],
        "attachments": [dump(a, ("content",)) for a in attachments],
    }


@router.post("/{identifier}/updates")
async def update(identifier: UUID, body: CaseNote, user: User, db: DB):
    row, item, thread = await owned(db, identifier, user)
    if body.status != row.status and user.id != thread.wakeel_id:
        raise HTTPException(403, "The assigned Wakeel manages case status.")
    row.status = body.status
    row.important_date = body.important_date.isoformat() if body.important_date else None
    db.add(
        CaseUpdate(
            case_id=row.id, actor_id=user.id, note=body.note, status=row.status, important_date=row.important_date
        )
    )
    notify(db, other_party(thread, user), "Case update: " + row.title, f"/cases/{row.id}")
    audit(db, user, "case_updated", row.id)
    return dump(row)


@router.post("/{identifier}/attachments", status_code=201)
async def upload(identifier: UUID, file: UploadFile, user: User, db: DB):
    row, _, thread = await owned(db, identifier, user)
    data = await file.read(5 * 1024 * 1024 + 1)
    await file.close()
    if len(data) > 5 * 1024 * 1024 or not data:
        raise HTTPException(413, "Upload a file between 1 byte and 5 MB.")
    mime = ""
    if data.startswith(b"%PDF-"):
        mime = "application/pdf"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    if not mime:
        raise HTTPException(415, "Upload a PDF, PNG or JPEG.")
    filename = Path((file.filename or "document").replace("\\", "/")).name
    filename = "".join(c for c in filename if c.isprintable())[:180] or "document"
    attachment = Attachment(case_id=row.id, uploader_id=user.id, filename=filename, content_type=mime, content=data)
    db.add(attachment)
    await db.flush()
    audit(db, user, "attachment_uploaded", attachment.id)
    notify(db, other_party(thread, user), "New case document", f"/cases/{row.id}")
    return dump(attachment, ("content",))


@router.get("/{identifier}/attachments/{attachment_id}")
async def download(identifier: UUID, attachment_id: UUID, user: User, db: DB):
    from urllib.parse import quote

    row, _, _ = await owned(db, identifier, user)
    file = await db.get(Attachment, str(attachment_id))
    if not file or file.case_id != row.id:
        raise HTTPException(404, "Attachment not found.")
    return Response(
        file.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file.filename),
            "Content-Security-Policy": "sandbox",
        },
    )
