from fastapi import HTTPException
from fastapi import Query
from typing import Annotated
from sqlalchemy import or_, select

from app.platform.models import Agreement, Conversation, WakeelProfile

Offset = Annotated[int, Query(ge=0)]


def dump(row, exclude=()):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in exclude}


async def conversation(db, identifier, user, lock=False):
    query = select(Conversation).where(
        Conversation.id == str(identifier), or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id)
    )
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query)
    if item is None:
        raise HTTPException(404, "Conversation not found.")
    return item


async def agreement(db, identifier, user, lock=False):
    """Service agreement plus its conversation, in one round trip."""
    query = (
        select(Agreement, Conversation)
        .join(Conversation)
        .where(
            Agreement.id == str(identifier),
            Agreement.kind == "service",
            or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id),
        )
    )
    if lock:
        query = query.with_for_update(of=Agreement)
    row = (await db.execute(query)).first()
    if not row:
        raise HTTPException(404, "Agreement not found.")
    item, thread = row
    if item.status == "DRAFT" and user.id != thread.wakeel_id:
        raise HTTPException(404, "Agreement not found.")
    return item, thread


async def verified_wakeel(db, user):
    if user.role != "wakeel":
        raise HTTPException(403, "Only a Wakeel can prepare an agreement.")
    profile = await db.get(WakeelProfile, user.id)
    if not profile or profile.verification != "verified":
        raise HTTPException(403, "Your professional profile must be verified first.")


def other_party(thread, user):
    return thread.wakeel_id if user.id == thread.user_id else thread.user_id


# ---------------------------------------------------------------- engagements
async def relationship(db, identifier, user, lock=False):
    from app.platform.models import Relationship

    query = select(Relationship).where(
        Relationship.id == str(identifier), or_(Relationship.user_id == user.id, Relationship.wakeel_id == user.id)
    )
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query)
    if item is None:
        raise HTTPException(404, "This workspace could not be found.")
    return item


def activity(db, relationship_id, user, summary, href=""):
    """Workspace timeline entry. Stored in the append-only audit table."""
    from app.platform.models import Audit

    if relationship_id:
        db.add(
            Audit(
                actor_id=user.id, action="workspace", resource_id=relationship_id, detail=(summary + "|" + href)[:900]
            )
        )


def money(value):
    return None if value is None else f"{value:.2f}"


def need_reason(text, minimum=5):
    if len(text.strip()) < minimum:
        raise HTTPException(422, f"Explain the reason in at least {minimum} characters.")
    return text.strip()


# ------------------------------------------------------------------ documents
MAX_FILE_BYTES = 5 * 1024 * 1024
DOCUMENT_KINDS = (
    "evidence",
    "court_filing",
    "identity",
    "contract",
    "correspondence",
    "invoice",
    "deliverable",
    "other",
)


def sniff(data: bytes, filename: str) -> str:
    """Trust file contents, not the browser-declared type."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"PK\x03\x04") and filename.lower().endswith(".docx") and b"word/" in data[:4096]:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    raise HTTPException(415, "This file type is not supported. Upload a PDF, Word (.docx), PNG or JPEG file.")


async def store_file(db, file, user, *, title="", kind="other", description="", **parent):
    from pathlib import Path

    from app.platform.models import Attachment

    data = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    if not data:
        raise HTTPException(400, "The selected file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "The selected file exceeds the 5 MB limit.")
    filename = Path((file.filename or "document").replace("\\", "/")).name
    filename = "".join(c for c in filename if c.isprintable())[:180] or "document"
    row = Attachment(
        uploader_id=user.id,
        filename=filename,
        content_type=sniff(data, filename),
        content=data,
        size=len(data),
        title=(title or "").strip()[:180] or filename,
        kind=kind if kind in DOCUMENT_KINDS else "other",
        description=(description or "").strip()[:2000],
        **parent,
    )
    db.add(row)
    await db.flush()
    return row


def file_response(file):
    from urllib.parse import quote

    from fastapi.responses import Response

    return Response(
        file.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file.filename),
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


def file_dump(file):
    return dump(file, ("content",))
