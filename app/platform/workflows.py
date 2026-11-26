"""Drafts and scheduled lifecycle changes share the transactional event broker."""

from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.platform.common import conversation, dump, verified_wakeel
from app.platform.events import invalidate
from app.platform.models import Account, Agreement, AgreementDraft, CaseAgreement, Conversation, now
from app.platform.schemas import DraftIn
from app.platform.security import DB, User, notify

router = APIRouter(tags=["workflow drafts"])


@router.get("/agreement-drafts")
async def drafts(user: User, db: DB):
    return [
        dump(row)
        for row in (
            await db.scalars(
                select(AgreementDraft)
                .where(AgreementDraft.user_id == user.id)
                .order_by(AgreementDraft.updated_at.desc())
                .limit(200)
            )
        ).all()
    ]


@router.get("/agreement-drafts/{identifier}")
async def draft(identifier: UUID, user: User, db: DB):
    await verified_wakeel(db, user)
    thread = await conversation(db, identifier, user)
    row = await db.get(AgreementDraft, thread.id)
    return dump(row) if row else {"conversation_id": thread.id, "revision": 0, "terms": {}}


@router.post("/agreement-drafts/{identifier}")
async def save_draft(identifier: UUID, body: DraftIn, user: User, db: DB):
    await verified_wakeel(db, user)
    thread = await conversation(db, identifier, user, lock=True)
    row = await db.get(AgreementDraft, thread.id)
    if body.revision != (row.revision if row else 0):
        raise HTTPException(409, "This draft changed in another tab. Reload before editing.")
    if not row:
        row = AgreementDraft(conversation_id=thread.id, user_id=user.id, revision=0)
        db.add(row)
    from app.platform.models import Preparation

    if body.preparation_id:
        preparation = await db.get(Preparation, str(body.preparation_id))
        if not preparation or preparation.conversation_id != thread.id:
            raise HTTPException(404, "Preparation request not found.")
    if body.case_id:
        from app.platform.cases import owned

        case = await owned(db, body.case_id, user)
        if case.user_id != thread.user_id or case.wakeel_id != thread.wakeel_id:
            raise HTTPException(409, "The draft and case must have the same parties.")
    row.preparation_id = str(body.preparation_id) if body.preparation_id else None
    row.case_id = str(body.case_id) if body.case_id else None
    row.terms, row.updated_at = body.terms, now()
    row.revision += 1
    invalidate(db, [user.id], "/agreement-drafts", f"/agreement-drafts/{thread.id}")
    await db.flush()
    return dump(row)


async def expire_agreements(database, hub):
    from app.platform.agreements import event

    today = now().astimezone(ZoneInfo("Asia/Karachi")).date().isoformat()
    async with database.session() as db, db.begin():
        rows = (
            await db.scalars(
                select(Agreement)
                .where(
                    Agreement.expires_on < today,
                    Agreement.active_version.is_not(None),
                    Agreement.status.in_(
                        ["ACTIVE", "CLOSURE_REQUESTED", "AMENDMENT_PENDING", "SIGNING_PENDING", "TERMINATION_PENDING"]
                    ),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            thread = await db.get(Conversation, row.conversation_id)
            actor = await db.get(Account, thread.wakeel_id)
            before = row.status
            row.status = "EXPIRED"
            row.revision += 1
            event(db, row, actor, "expire", before, "System: the signed agreement end date has passed.")
            for recipient in (thread.user_id, thread.wakeel_id):
                await notify(db, recipient, "Agreement expired: " + row.title, f"/agreements/{row.id}")
            cases = (await db.scalars(select(CaseAgreement.case_id).where(CaseAgreement.agreement_id == row.id))).all()
            invalidate(db, [thread.user_id, thread.wakeel_id], "/cases", *[f"/cases/{cid}" for cid in cases])
        events = db.info.get("platform_events", [])
        if events:
            await hub.publish(db, events)
    if events:
        await hub.after_commit(events)
