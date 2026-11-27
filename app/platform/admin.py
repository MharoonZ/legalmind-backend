from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, func, select

from app.platform.common import Offset, agreement, conversation, dump, other_party
from app.platform.agreements import event
from app.platform.models import (
    Acceptance,
    Account,
    Agreement,
    Audit,
    Chat,
    Conversation,
    Notification,
    Report,
    Review,
    WakeelProfile,
    now,
)
from app.platform.schemas import AdminAction, Announcement, ReportIn, ReviewIn
from app.platform.security import DB, User, audit, notify, require_role, revoke_sessions

from app.platform.events import directory_changed, invalidate

router = APIRouter(tags=["notifications, reviews and platform administration"])


@router.get("/notifications")
async def notifications(user: User, db: DB, offset: Offset = 0):
    return [
        dump(n)
        for n in (
            await db.scalars(
                select(Notification)
                .where(Notification.user_id == user.id)
                .order_by(Notification.created_at.desc())
                .offset(offset)
                .limit(200)
            )
        ).all()
    ]


@router.get("/notifications/count")
async def notification_count(user: User, db: DB):
    """Navigation badges in one round trip: notifications, general-chat messages, open hiring requests."""
    from sqlalchemy import or_

    from app.platform.models import HiringRequest, Message

    def total(source, *conditions):
        return select(func.count()).select_from(source).where(*conditions).scalar_subquery()

    threads = select(Conversation.id).where(or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id))
    row = (
        await db.execute(
            select(
                total(Notification, Notification.user_id == user.id, Notification.read_at.is_(None)),
                total(
                    Message,
                    Message.conversation_id.in_(threads),
                    Message.sender_id != user.id,
                    Message.read_at.is_(None),
                    Message.case_id.is_(None),
                ),
                total(
                    HiringRequest,
                    or_(HiringRequest.user_id == user.id, HiringRequest.wakeel_id == user.id),
                    HiringRequest.status.in_(["PENDING", "UNDER_DISCUSSION"]),
                ),
            )
        )
    ).one()
    return {"unread": row[0], "messages": row[1], "hiring": row[2]}


@router.post("/notifications/{identifier}/read")
async def read(identifier: UUID, user: User, db: DB):
    item = await db.get(Notification, str(identifier))
    if not item or item.user_id != user.id:
        raise HTTPException(404, "Notification not found.")
    item.read_at = now()
    invalidate(db, [user.id], "/notifications", "/dashboard")
    return {"ok": True}


@router.post("/notifications/read-all")
async def read_all(user: User, db: DB):
    from sqlalchemy import update

    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=now())
    )
    invalidate(db, [user.id], "/notifications", "/dashboard")
    return {"ok": True}


@router.get("/reviews")
async def reviews(user: User, db: DB):
    from app.platform.marketplace import public_reviews

    return await public_reviews(db, user.id, 200)


@router.post("/reviews", status_code=201)
async def rate(body: ReviewIn, user: User, db: DB):
    item, thread = await agreement(db, body.agreement_id, user, lock=True)
    if item.status != "COMPLETED":
        raise HTTPException(409, "Reviews are available after both parties confirm completion.")
    if await db.scalar(select(Review.id).where(Review.agreement_id == item.id, Review.author_id == user.id)):
        raise HTTPException(409, "You have already reviewed this engagement.")
    row = Review(
        agreement_id=item.id,
        author_id=user.id,
        subject_id=other_party(thread, user),
        rating=body.rating,
        body=body.body,
    )
    db.add(row)
    await db.flush()
    directory_changed(db)
    invalidate(db, [row.subject_id], "/reviews")
    audit(db, user, "review_added", row.id)
    return dump(row)


@router.post("/reports", status_code=201)
async def report(body: ReportIn, user: User, db: DB):
    thread = await conversation(db, body.conversation_id, user)
    row = Report(reporter_id=user.id, conversation_id=thread.id, reason=body.reason)
    db.add(row)
    await db.flush()
    audit(db, user, "content_reported", row.id)
    return dump(row)


@router.get("/admin")
async def overview(user: User, db: DB, offset: Offset = 0):
    require_role(user, "admin")
    accounts = (await db.scalars(select(Account).order_by(Account.created_at.desc()).offset(offset).limit(200))).all()
    profiles = (
        await db.scalars(select(WakeelProfile).where(WakeelProfile.user_id.in_([u.id for u in accounts])))
    ).all()
    reports = (await db.scalars(select(Report).order_by(Report.created_at.desc()).offset(offset).limit(200))).all()
    disputes = (
        await db.scalars(
            select(Agreement)
            .where(Agreement.status == "DISPUTED")
            .order_by(Agreement.updated_at.desc())
            .offset(offset)
            .limit(200)
        )
    ).all()
    logs = (await db.scalars(select(Audit).order_by(Audit.created_at.desc()).offset(offset).limit(200))).all()
    from app.platform.models import Case, HiringRequest, Relationship

    count_models = (Account, Chat, Conversation, Agreement, Report, Case, HiringRequest, Relationship)
    counts = dict(
        (
            await db.execute(
                select(
                    *[
                        select(func.count()).select_from(model).scalar_subquery().label(model.__tablename__)
                        for model in count_models
                    ]
                )
            )
        )
        .mappings()
        .one()
    )
    return {
        "users": [dump(u, ("password_hash",)) for u in accounts],
        "wakeels": [dump(p) for p in profiles],
        "reports": [dump(r) for r in reports],
        "disputes": [dump(d) for d in disputes],
        "audit": [dump(a) for a in logs],
        "counts": counts,
    }


RECORDS = {
    "hiring": (
        "platform_hiring_requests",
        ("id", "user_id", "wakeel_id", "category", "status", "created_at", "updated_at"),
    ),
    "relationships": ("platform_relationships", ("id", "user_id", "wakeel_id", "status", "created_at", "ended_at")),
    "cases": (
        "platform_cases",
        ("id", "user_id", "wakeel_id", "title", "category", "status", "payment_status", "created_at"),
    ),
    "agreements": (
        "platform_agreements",
        ("id", "creator_id", "title", "kind", "category", "status", "created_at", "updated_at"),
    ),
}


@router.get("/admin/records")
async def records(kind: str, user: User, db: DB, offset: Offset = 0):
    """Oversight metadata only. Descriptions, messages, terms and documents stay private to participants."""
    from app.platform import models

    require_role(user, "admin")
    if kind not in RECORDS:
        raise HTTPException(404, "Unknown record type.")
    table, columns = RECORDS[kind]
    model = next(m.class_ for m in models.Base.registry.mappers if m.class_.__tablename__ == table)
    rows = (await db.scalars(select(model).order_by(model.created_at.desc()).offset(offset).limit(100))).all()
    ids = {getattr(r, c) for r in rows for c in ("user_id", "wakeel_id", "creator_id") if getattr(r, c, None)}
    people = dict((await db.execute(select(Account.id, Account.name).where(Account.id.in_(ids or [""])))).all())
    audit(db, user, "admin_records_viewed", None, kind)
    return {
        "rows": [{c: getattr(r, c) for c in columns} for r in rows],
        "people": people,
        "has_more": len(rows) == 100,
    }


@router.get("/admin/settings")
async def platform_settings(user: User):
    """Read-only operational status. Secrets are never returned."""
    from app.core.config import get_settings
    from app.platform.mail import configured

    require_role(user, "admin")
    settings = get_settings()
    return {
        "environment": settings.environment,
        "email_delivery_configured": configured(settings),
        "password_recovery_available": configured(settings) and len(settings.otp_secret) >= 32,
        "email_notifications_enabled": settings.email_notifications_enabled,
        "otp_expiry_minutes": settings.otp_expiry_minutes,
        "otp_max_attempts": settings.otp_max_attempts,
        "password_min_length": settings.password_min_length,
        "max_document_mb": 5,
    }


@router.post("/admin/announcements", status_code=201)
async def announce(body: Announcement, user: User, db: DB):
    require_role(user, "admin")
    query = select(Account.id).where(Account.active.is_(True), Account.role != "admin")
    if body.audience != "all":
        query = query.where(Account.role == body.audience)
    recipients = (await db.scalars(query.limit(5000))).all()
    db.add_all([Notification(user_id=uid, title=body.title, href="/notifications") for uid in recipients])
    invalidate(db, list(recipients), "/notifications", "/dashboard")
    audit(db, user, "admin_announcement", None, f"{body.audience}: {body.title}")
    return {"sent": len(recipients)}


@router.get("/admin/reports/{identifier}")
async def report_detail(identifier: UUID, user: User, db: DB, offset: Offset = 0):
    from app.platform.models import Message

    require_role(user, "admin")
    row = await db.get(Report, str(identifier))
    if not row:
        raise HTTPException(404, "Report not found.")
    messages = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == row.conversation_id)
            .order_by(Message.created_at)
            .offset(offset)
            .limit(200)
        )
    ).all()
    audit(db, user, "reported_content_reviewed", row.id)
    return {**dump(row), "messages": [dump(m) for m in messages]}


@router.get("/admin/disputes/{identifier}")
async def dispute_detail(identifier: UUID, user: User, db: DB):
    from app.platform.models import AgreementEvent, AgreementVersion

    require_role(user, "admin")
    row = await db.get(Agreement, str(identifier))
    if not row or row.status != "DISPUTED":
        raise HTTPException(404, "Dispute not found.")
    audit(db, user, "dispute_reviewed", row.id)
    result = dump(row)
    for name, model in (("versions", AgreementVersion), ("events", AgreementEvent), ("acceptances", Acceptance)):
        result[name] = [dump(i) for i in (await db.scalars(select(model).where(model.agreement_id == row.id))).all()]
    return result


@router.post("/admin/actions")
async def administer(body: AdminAction, user: User, db: DB):
    require_role(user, "admin")
    identifier = str(body.resource_id)
    if body.action in {"verify", "reject"}:
        profile = await db.scalar(select(WakeelProfile).where(WakeelProfile.user_id == identifier).with_for_update())
        if not profile:
            raise HTTPException(404, "Wakeel not found.")
        profile.verification = "verified" if body.action == "verify" else "rejected"
        profile.verification_reason = body.reason
        await notify(db, identifier, "Professional verification updated", "/profile")
    elif body.action in {"suspend", "restore"}:
        target = await db.get(Account, identifier)
        if not target or target.role == "admin":
            raise HTTPException(403, "This account cannot be changed here.")
        target.active = body.action == "restore"
        await revoke_sessions(db, user_id=target.id)
    elif body.action == "resolve_report":
        row = await db.get(Report, identifier)
        if not row:
            raise HTTPException(404, "Report not found.")
        row.status, row.resolution = "RESOLVED", body.reason
        await notify(db, row.reporter_id, "Your report was reviewed: " + body.reason[:100], "/notifications")
    elif body.action == "resolve_dispute":
        row = await db.scalar(select(Agreement).where(Agreement.id == identifier).with_for_update())
        if not row or row.status != "DISPUTED" or body.outcome is None:
            raise HTTPException(409, "Select an outcome for an open dispute.")
        row.status = body.outcome
        row.current_version = row.active_version
        row.revision += 1
        # A reopened engagement needs fresh completion confirmations.
        await db.execute(delete(Acceptance).where(Acceptance.agreement_id == row.id, Acceptance.kind == "completion"))
        event(db, row, user, "dispute_resolved", "DISPUTED", body.reason)
        thread = await db.get(Conversation, row.conversation_id)
        for recipient in (thread.user_id, thread.wakeel_id):
            await notify(db, recipient, "Contract dispute resolved", f"/agreements/{row.id}")
    if body.action in {"verify", "reject", "suspend", "restore"}:
        directory_changed(db)
    audit(db, user, "admin_" + body.action, identifier, body.reason)
    return {"ok": True}
