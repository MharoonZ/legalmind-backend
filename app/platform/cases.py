from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import defer

from app.platform.common import Offset, agreement, dump, verified_wakeel
from app.platform.models import Account, Agreement, Attachment, Case, CaseAgreement, CaseUpdate, Conversation
from app.platform.schemas import CaseAssignment, CaseIn, CaseLink, CaseNote
from app.platform.security import DB, User, audit, notify

router = APIRouter(prefix="/cases", tags=["case management"])


async def owned(db, identifier, user):
    row = await db.scalar(
        select(Case)
        .where(Case.id == str(identifier), or_(Case.user_id == user.id, Case.wakeel_id == user.id))
        .with_for_update()
    )
    if not row:
        raise HTTPException(404, "Case not found.")
    return row


async def changed(db, row, user, title):
    from app.platform.events import invalidate

    parties = [uid for uid in (row.user_id, row.wakeel_id) if uid]
    invalidate(db, parties, "/cases", f"/cases/{row.id}", "/agreements")
    for recipient in parties:
        if recipient != user.id:
            await notify(db, recipient, title, f"/cases/{row.id}")
    from app.platform.mail import enqueue

    await enqueue(db, user.id, "cases", title, f"/cases/{row.id}")
    agreement_ids = (await db.scalars(select(CaseAgreement.agreement_id).where(CaseAgreement.case_id == row.id))).all()
    invalidate(db, parties, *[f"/agreements/{aid}" for aid in agreement_ids])


async def link_agreement(db, row, item, thread, user):
    if row.user_id != thread.user_id or row.wakeel_id != thread.wakeel_id:
        raise HTTPException(409, "Case and agreement must have the same client and assigned Wakeel.")
    if await db.get(CaseAgreement, (row.id, item.id)):
        return
    db.add(CaseAgreement(case_id=row.id, agreement_id=item.id, linked_by=user.id))
    if item.status != "DRAFT":
        db.add(
            CaseUpdate(
                case_id=row.id,
                actor_id=user.id,
                note="Linked agreement: " + item.title,
                status=row.status,
                important_date=row.important_date,
            )
        )
    await db.flush()
    audit(db, user, "case_agreement_linked", row.id, item.id)


@router.get("")
async def cases(user: User, db: DB, offset: Offset = 0):
    query = select(Case).where(or_(Case.user_id == user.id, Case.wakeel_id == user.id))
    return [dump(c) for c in (await db.scalars(query.order_by(Case.created_at.desc()).offset(offset).limit(200))).all()]


@router.post("", status_code=201)
async def create(body: CaseIn, user: User, db: DB):
    item = thread = None
    if body.agreement_id:
        item, thread = await agreement(db, body.agreement_id, user)
        if item.status == "DRAFT":
            raise HTTPException(409, "Share the agreement before opening a linked case.")
        owner, wakeel = thread.user_id, thread.wakeel_id
    else:
        if user.role != "user":
            raise HTTPException(403, "Clients open independent cases. Wakeels can open a case from a shared agreement.")
        owner, wakeel = user.id, str(body.wakeel_id) if body.wakeel_id else None
        if wakeel:
            assignee = await db.get(Account, wakeel)
            if not assignee or not assignee.active:
                raise HTTPException(404, "Wakeel not found.")
            await verified_wakeel(db, assignee)
    row = Case(
        agreement_id=item.id if item else None,
        user_id=owner,
        wakeel_id=wakeel,
        title=body.title,
        description=body.description,
        status="ASSIGNED" if wakeel else "DRAFT",
        important_date=body.important_date.isoformat() if body.important_date else None,
    )
    db.add(row)
    await db.flush()
    if item:
        await link_agreement(db, row, item, thread, user)
    db.add(
        CaseUpdate(
            case_id=row.id, actor_id=user.id, note="Case opened", status=row.status, important_date=row.important_date
        )
    )
    audit(db, user, "case_created", row.id)
    await changed(db, row, user, "A case was opened")
    return dump(row)


@router.get("/{identifier}")
async def detail(identifier: UUID, user: User, db: DB):
    row = await owned(db, identifier, user)
    updates = (
        await db.scalars(select(CaseUpdate).where(CaseUpdate.case_id == row.id).order_by(CaseUpdate.created_at))
    ).all()
    attachments = (
        await db.scalars(select(Attachment).options(defer(Attachment.content)).where(Attachment.case_id == row.id))
    ).all()
    linked = (
        await db.scalars(
            select(Agreement)
            .join(CaseAgreement)
            .where(CaseAgreement.case_id == row.id, Agreement.status != "DRAFT" if user.id == row.user_id else True)
        )
    ).all()
    people_ids = {uid for uid in (row.user_id, row.wakeel_id) if uid} | {u.actor_id for u in updates}
    people = (await db.scalars(select(Account).where(Account.id.in_(people_ids)))).all()
    thread = (
        await db.scalar(
            select(Conversation).where(Conversation.user_id == row.user_id, Conversation.wakeel_id == row.wakeel_id)
        )
        if row.wakeel_id
        else None
    )
    return {
        **dump(row),
        "conversation_id": thread.id if thread else None,
        "allowed_statuses": sorted(TRANSITIONS.get(row.status, set())),
        "updates": [dump(u) for u in updates],
        "attachments": [dump(a, ("content",)) for a in attachments],
        "agreements": [dump(a) for a in linked],
        "people": {p.id: p.name for p in people},
    }


TRANSITIONS = {
    "DRAFT": {"SUBMITTED", "CLOSED"},
    "OPEN": {"UNDER_REVIEW", "IN_PROGRESS", "ON_HOLD", "CLOSED"},
    "SUBMITTED": {"UNDER_REVIEW", "ASSIGNED", "ON_HOLD", "DISMISSED"},
    "UNDER_REVIEW": {"ASSIGNED", "IN_PROGRESS", "ON_HOLD", "DISMISSED"},
    "ASSIGNED": {"UNDER_REVIEW", "IN_PROGRESS", "ON_HOLD", "CLOSED"},
    "IN_PROGRESS": {"AWAITING_HEARING", "AWAITING_DECISION", "ON_HOLD", "RESOLVED", "DISMISSED", "CLOSED"},
    "AWAITING_HEARING": {"HEARING_COMPLETED", "ON_HOLD"},
    "HEARING_COMPLETED": {"AWAITING_HEARING", "AWAITING_DECISION", "IN_PROGRESS"},
    "AWAITING_DECISION": {"RESOLVED", "DISMISSED", "AWAITING_HEARING", "ON_HOLD"},
    "ON_HOLD": {"UNDER_REVIEW", "IN_PROGRESS", "CLOSED"},
    "RESOLVED": {"CLOSED", "REOPENED"},
    "DISMISSED": {"CLOSED", "REOPENED"},
    "CLOSED": {"REOPENED"},
    "REOPENED": {"UNDER_REVIEW", "IN_PROGRESS", "ON_HOLD", "CLOSED"},
}


@router.post("/{identifier}/updates")
async def update(identifier: UUID, body: CaseNote, user: User, db: DB):
    row = await owned(db, identifier, user)
    date_value = body.important_date.isoformat() if body.important_date else None
    if (body.status != row.status or date_value != row.important_date) and user.id != (row.wakeel_id or row.user_id):
        raise HTTPException(403, "The assigned Wakeel manages case status and hearing dates.")
    if body.status != row.status and body.status not in TRANSITIONS.get(row.status, set()):
        raise HTTPException(409, "This case status transition is unavailable.")
    row.status, row.important_date = body.status, date_value
    db.add(
        CaseUpdate(
            case_id=row.id, actor_id=user.id, note=body.note, status=row.status, important_date=row.important_date
        )
    )
    await changed(db, row, user, "Case update: " + row.title)
    audit(db, user, "case_updated", row.id)
    return dump(row)


@router.post("/{identifier}/agreements")
async def link(identifier: UUID, body: CaseLink, user: User, db: DB):
    row = await owned(db, identifier, user)
    item, thread = await agreement(db, body.agreement_id, user)
    if item.status == "DRAFT":
        raise HTTPException(409, "Share the agreement before linking it.")
    await link_agreement(db, row, item, thread, user)
    await changed(db, row, user, "An agreement was linked to your case")
    return {"ok": True}


@router.post("/{identifier}/assignment")
async def assign(identifier: UUID, body: CaseAssignment, user: User, db: DB):
    row = await owned(db, identifier, user)
    if user.id != row.user_id:
        raise HTTPException(403, "Only the case owner can assign a Wakeel.")
    wakeel = await db.get(Account, str(body.wakeel_id))
    if not wakeel or not wakeel.active:
        raise HTTPException(404, "Wakeel not found.")
    await verified_wakeel(db, wakeel)
    if row.wakeel_id == wakeel.id:
        return dump(row)
    if await db.scalar(select(CaseAgreement.case_id).where(CaseAgreement.case_id == row.id).limit(1)):
        raise HTTPException(
            409, "This case has agreements with its assigned Wakeel. Open a separate case to work with another Wakeel."
        )
    previous = row.wakeel_id
    row.wakeel_id = wakeel.id
    if row.status in {"DRAFT", "SUBMITTED", "UNDER_REVIEW"}:
        row.status = "ASSIGNED"
    db.add(
        CaseUpdate(
            case_id=row.id,
            actor_id=user.id,
            note=f"Assigned to {wakeel.name}. {body.reason}",
            status=row.status,
            important_date=row.important_date,
        )
    )
    audit(db, user, "case_assigned", row.id, f"{previous or 'unassigned'} -> {wakeel.id}")
    await changed(db, row, user, "You were assigned a case")
    if previous:
        await notify(db, previous, "Your case assignment ended", "/cases")
    return dump(row)


@router.post("/{identifier}/attachments", status_code=201)
async def upload(identifier: UUID, file: UploadFile, user: User, db: DB):
    row = await owned(db, identifier, user)
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
    db.add(
        CaseUpdate(
            case_id=row.id,
            actor_id=user.id,
            note="Document uploaded: " + filename,
            status=row.status,
            important_date=row.important_date,
        )
    )
    await db.flush()
    audit(db, user, "attachment_uploaded", attachment.id)
    await changed(db, row, user, "New case document")
    return dump(attachment, ("content",))


@router.get("/{identifier}/attachments/{attachment_id}")
async def download(identifier: UUID, attachment_id: UUID, user: User, db: DB):
    from urllib.parse import quote

    row = await owned(db, identifier, user)
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
