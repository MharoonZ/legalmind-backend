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
from app.platform.schemas import AdminAction, ReportIn, ReviewIn
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


@router.post("/notifications/{identifier}/read")
async def read(identifier: UUID, user: User, db: DB):
    item = await db.get(Notification, str(identifier))
    if not item or item.user_id != user.id:
        raise HTTPException(404, "Notification not found.")
    item.read_at = now()
    invalidate(db, [user.id], "/notifications")
    return {"ok": True}


@router.get("/reviews")
async def reviews(user: User, db: DB, offset: Offset = 0):
    return [
        dump(r)
        for r in (
            await db.scalars(
                select(Review)
                .where(Review.subject_id == user.id)
                .order_by(Review.created_at.desc())
                .offset(offset)
                .limit(200)
            )
        ).all()
    ]


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
    count_models = (Account, Chat, Conversation, Agreement, Report)
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
        notify(db, identifier, "Professional verification updated", "/profile")
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
        notify(db, row.reporter_id, "Your report was reviewed: " + body.reason[:100], "/notifications")
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
            notify(db, recipient, "Contract dispute resolved", f"/agreements/{row.id}")
    if body.action in {"verify", "reject", "suspend", "restore"}:
        directory_changed(db)
    audit(db, user, "admin_" + body.action, identifier, body.reason)
    return {"ok": True}
