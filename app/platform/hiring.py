"""Hiring requests and client-Wakeel engagements (shared workspaces).

Each accepted hiring request creates one independent relationship. Ending an
engagement never deletes or closes its cases, agreements, messages or files.
"""

from datetime import UTC, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import case as sql_case
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from app.platform.common import (
    Offset,
    activity,
    dump,
    file_dump,
    need_reason,
    relationship,
    store_file,
    verified_wakeel,
)
from app.platform.events import directory_changed, invalidate
from app.platform.models import (
    Account,
    Agreement,
    AgreementParty,
    Attachment,
    Audit,
    Case,
    CasePayment,
    Conversation,
    HiringRequest,
    Rating,
    Relationship,
    WakeelProfile,
    now,
)
from app.platform.schemas import HiringAction, HiringIn, RelationshipAction, RelationshipRatingIn
from app.platform.security import DB, User, audit, notify, require_role, throttle

router = APIRouter(tags=["hiring and engagements"])

OPEN_REQUESTS = ("PENDING", "UNDER_DISCUSSION")
# Matters that still need attention before an engagement can end.
OPEN_CASES = (
    "SUBMITTED",
    "UNDER_DISCUSSION",
    "CHANGES_REQUESTED",
    "ACCEPTED",
    "ACTIVE",
    "IN_PROGRESS",
    "ON_HOLD",
    "AWAITING_CLIENT_INFORMATION",
    "AWAITING_WAKEEL_ACTION",
    "AWAITING_HEARING",
    "HEARING_COMPLETED",
    "AWAITING_DECISION",
    "COMPLETION_REQUESTED",
)
UNILATERAL_AFTER = timedelta(days=7)


def peer_of(row, user):
    return row.wakeel_id if user.id == row.user_id else row.user_id


async def names(db, identifiers):
    identifiers = {i for i in identifiers if i}
    if not identifiers:
        return {}
    rows = await db.execute(select(Account.id, Account.name).where(Account.id.in_(identifiers)))
    return dict(rows.all())


async def hiring(db, identifier, user, lock=False):
    query = select(HiringRequest).where(
        HiringRequest.id == str(identifier), or_(HiringRequest.user_id == user.id, HiringRequest.wakeel_id == user.id)
    )
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if not row:
        raise HTTPException(404, "This hiring request could not be found.")
    return row


def touch(db, row, *extra):
    invalidate(
        db,
        [row.user_id, row.wakeel_id],
        "/hiring-requests",
        "/relationships",
        "/dashboard",
        *extra,
    )


# ------------------------------------------------------------ hiring requests
@router.post("/hiring-requests", status_code=201)
async def create_request(body: HiringIn, request: Request, user: User, db: DB):
    require_role(user, "user")
    await throttle(request, "hiring", user.id, 10)
    wakeel = await db.get(Account, str(body.wakeel_id))
    profile = await db.get(WakeelProfile, str(body.wakeel_id))
    if not wakeel or not wakeel.active or not profile or profile.verification != "verified":
        raise HTTPException(404, "This Wakeel is not available for hiring.")
    if await db.scalar(
        select(Relationship.id).where(
            Relationship.user_id == user.id, Relationship.wakeel_id == wakeel.id, Relationship.status != "ENDED"
        )
    ):
        raise HTTPException(409, "You already have an active engagement with this Wakeel. Open your shared workspace.")
    if await db.scalar(
        select(HiringRequest.id).where(
            HiringRequest.user_id == user.id,
            HiringRequest.wakeel_id == wakeel.id,
            HiringRequest.status.in_(OPEN_REQUESTS),
        )
    ):
        raise HTTPException(409, "You already have a pending hiring request with this Wakeel.")
    pair = select(Conversation).where(Conversation.user_id == user.id, Conversation.wakeel_id == wakeel.id)
    thread = await db.scalar(pair)
    if not thread:
        try:
            async with db.begin_nested():
                thread = Conversation(user_id=user.id, wakeel_id=wakeel.id)
                db.add(thread)
                await db.flush()
        except IntegrityError:
            thread = await db.scalar(pair)
    row = HiringRequest(
        user_id=user.id,
        wakeel_id=wakeel.id,
        conversation_id=thread.id,
        **body.model_dump(exclude={"wakeel_id"}),
    )
    db.add(row)
    await db.flush()
    audit(db, user, "hiring_requested", row.id)
    await notify(db, wakeel.id, f"New hiring request from {user.name}", f"/hiring/{row.id}")
    touch(db, row, "/conversations")
    return dump(row)


@router.get("/hiring-requests")
async def list_requests(user: User, db: DB, offset: Offset = 0, status: Annotated[str, Query(max_length=20)] = ""):
    query = select(HiringRequest).where(or_(HiringRequest.user_id == user.id, HiringRequest.wakeel_id == user.id))
    if status == "open":
        query = query.where(HiringRequest.status.in_(OPEN_REQUESTS))
    elif status:
        query = query.where(HiringRequest.status == status.upper())
    rows = (await db.scalars(query.order_by(HiringRequest.updated_at.desc()).offset(offset).limit(200))).all()
    people = await names(db, {peer_of(row, user) for row in rows})
    return [{**dump(row), "peer_name": people.get(peer_of(row, user), "")} for row in rows]


@router.get("/hiring-requests/{identifier}")
async def request_detail(identifier: UUID, user: User, db: DB):
    row = await hiring(db, identifier, user)
    files = (
        await db.scalars(
            select(Attachment).options(defer(Attachment.content)).where(Attachment.hiring_request_id == row.id)
        )
    ).all()
    actions = []
    if row.status in OPEN_REQUESTS:
        if user.id == row.wakeel_id:
            actions = ["accept", "reject"] + (["discuss"] if row.status == "PENDING" else [])
        else:
            actions = ["cancel"]
    return {
        **dump(row),
        "people": await names(db, {row.user_id, row.wakeel_id}),
        "documents": [file_dump(f) for f in files],
        "actions": actions,
    }


@router.post("/hiring-requests/{identifier}/actions")
async def request_action(identifier: UUID, body: HiringAction, user: User, db: DB):
    row = await hiring(db, identifier, user, lock=True)
    if row.status not in OPEN_REQUESTS:
        raise HTTPException(409, "This hiring request has already been " + row.status.lower().replace("_", " ") + ".")
    wakeel = user.id == row.wakeel_id
    other = peer_of(row, user)
    href = f"/hiring/{row.id}"
    if body.action == "cancel":
        if wakeel:
            raise HTTPException(403, "Only the client can cancel a hiring request.")
        row.status = "CANCELLED"
        row.response_reason = body.reason
        await notify(db, other, f"{user.name} cancelled a hiring request", href)
    elif not wakeel:
        raise HTTPException(403, "Only the Wakeel can respond to a hiring request.")
    elif body.action == "discuss":
        if row.status != "PENDING":
            raise HTTPException(409, "This request is already under discussion.")
        row.status = "UNDER_DISCUSSION"
        await notify(db, other, f"{user.name} wants to discuss your hiring request", href)
    elif body.action == "reject":
        row.response_reason = need_reason(body.reason)
        row.status = "REJECTED"
        await notify(db, other, f"{user.name} declined your hiring request", href)
    else:
        await verified_wakeel(db, user)
        engagement = Relationship(
            user_id=row.user_id,
            wakeel_id=row.wakeel_id,
            hiring_request_id=row.id,
            conversation_id=row.conversation_id,
        )
        try:
            async with db.begin_nested():
                db.add(engagement)
                await db.flush()
        except IntegrityError:
            raise HTTPException(409, "An active engagement with this client already exists.") from None
        row.status, row.relationship_id, row.response_reason = "ACCEPTED", engagement.id, body.reason
        activity(db, engagement.id, user, "Engagement started: " + row.category, f"/workspaces/{engagement.id}")
        await notify(db, other, f"{user.name} accepted your hiring request", f"/workspaces/{engagement.id}")
    row.updated_at = now()
    audit(db, user, "hiring_" + body.action, row.id, body.reason[:300])
    touch(db, row, f"/hiring-requests/{row.id}")
    return dump(row)


@router.post("/hiring-requests/{identifier}/documents", status_code=201)
async def request_document(
    identifier: UUID,
    file: UploadFile,
    user: User,
    db: DB,
    title: Annotated[str, Form(max_length=180)] = "",
    kind: Annotated[str, Form(max_length=40)] = "other",
    description: Annotated[str, Form(max_length=2000)] = "",
):
    row = await hiring(db, identifier, user, lock=True)
    if user.id != row.user_id or row.status not in OPEN_REQUESTS:
        raise HTTPException(403, "Supporting documents can be added by the client while the request is open.")
    stored = await store_file(db, file, user, title=title, kind=kind, description=description, hiring_request_id=row.id)
    audit(db, user, "document_uploaded", stored.id, "hiring request")
    touch(db, row, f"/hiring-requests/{row.id}", "/documents")
    return file_dump(stored)


# -------------------------------------------------------------- relationships
async def summary(db, row, user):
    """Checklist both parties review before an engagement ends."""
    cases = (
        await db.execute(
            select(Case.id, Case.title, Case.status, Case.agreed_fee, Case.currency).where(
                Case.relationship_id == row.id
            )
        )
    ).all()
    paid = dict(
        (
            await db.execute(
                select(CasePayment.case_id, func.sum(CasePayment.amount))
                .where(CasePayment.case_id.in_([c.id for c in cases] or [""]), CasePayment.status == "CONFIRMED")
                .group_by(CasePayment.case_id)
            )
        ).all()
    )
    pending = await db.scalar(
        select(func.count())
        .select_from(CasePayment)
        .where(CasePayment.case_id.in_([c.id for c in cases] or [""]), CasePayment.status == "PENDING")
    )
    totals = {}
    for item in cases:
        if item.agreed_fee is None or item.status in ("REJECTED", "CANCELLED", "DRAFT"):
            continue
        bucket = totals.setdefault(item.currency, {"agreed": 0.0, "paid": 0.0})
        bucket["agreed"] += float(item.agreed_fee)
        bucket["paid"] += float(paid.get(item.id) or 0)
    for bucket in totals.values():
        bucket["outstanding"] = max(0.0, bucket["agreed"] - bucket["paid"])
    return {
        "open_cases": [{"id": c.id, "title": c.title, "status": c.status} for c in cases if c.status in OPEN_CASES],
        "case_count": len(cases),
        "pending_payments": pending or 0,
        "financial": totals,
    }


def relationship_actions(row, user):
    if row.status == "ENDED":
        return []
    if row.status == "ACTIVE":
        return ["request_termination"]
    if row.termination_requested_by == user.id:
        actions = ["withdraw_termination"]
        requested = row.termination_requested_at
        if requested and now() - requested.replace(tzinfo=UTC) >= UNILATERAL_AFTER:
            actions.append("confirm_termination")
        return actions
    return (["review_termination"] if row.status == "TERMINATION_REQUESTED" else []) + ["confirm_termination"]


@router.get("/relationships")
async def relationships(user: User, db: DB, offset: Offset = 0, status: Annotated[str, Query(max_length=24)] = ""):
    open_cases = (
        select(func.count())
        .select_from(Case)
        .where(Case.relationship_id == Relationship.id, Case.status.in_(OPEN_CASES))
        .correlate(Relationship)
        .scalar_subquery()
    )
    peer_id = sql_case((Relationship.user_id == user.id, Relationship.wakeel_id), else_=Relationship.user_id)
    query = (
        select(Relationship, Account.name, Account.city, WakeelProfile.specialization, open_cases)
        .join(Account, Account.id == peer_id)
        .outerjoin(WakeelProfile, WakeelProfile.user_id == Relationship.wakeel_id)
        .where(or_(Relationship.user_id == user.id, Relationship.wakeel_id == user.id))
    )
    if status == "open":
        query = query.where(Relationship.status != "ENDED")
    elif status:
        query = query.where(Relationship.status == status.upper())
    rows = (await db.execute(query.order_by(Relationship.updated_at.desc()).offset(offset).limit(200))).all()
    return [
        {**dump(row), "peer_name": name, "peer_city": city, "specialization": specialization, "open_cases": count}
        for row, name, city, specialization, count in rows
    ]


@router.get("/relationships/{identifier}")
async def relationship_detail(identifier: UUID, user: User, db: DB):
    row = await relationship(db, identifier, user)
    request = await db.get(HiringRequest, row.hiring_request_id) if row.hiring_request_id else None
    cases = (
        await db.scalars(select(Case).where(Case.relationship_id == row.id).order_by(Case.updated_at.desc()).limit(200))
    ).all()
    # Service agreements from this pair's thread plus multi-party agreements linked to the workspace.
    visible = or_(
        Agreement.conversation_id == row.conversation_id,
        (Agreement.relationship_id == row.id)
        & Agreement.id.in_(
            select(AgreementParty.agreement_id).where(
                AgreementParty.account_id == user.id, AgreementParty.invite_status == "ACCEPTED"
            )
        ),
        (Agreement.relationship_id == row.id) & (Agreement.creator_id == user.id),
    )
    query = select(Agreement).where(visible)
    if user.id == row.user_id:
        query = query.where(or_(Agreement.status != "DRAFT", Agreement.creator_id == user.id))
    agreements = (await db.scalars(query.order_by(Agreement.updated_at.desc()).limit(200))).all()
    case_ids = [c.id for c in cases]
    files = (
        await db.scalars(
            select(Attachment)
            .options(defer(Attachment.content))
            .where(
                or_(
                    Attachment.relationship_id == row.id,
                    Attachment.case_id.in_(case_ids or [""]),
                    Attachment.hiring_request_id == (row.hiring_request_id or ""),
                )
            )
            .order_by(Attachment.created_at.desc())
            .limit(200)
        )
    ).all()
    timeline = (
        await db.scalars(
            select(Audit)
            .where(Audit.resource_id == row.id, Audit.action == "workspace")
            .order_by(Audit.created_at.desc())
            .limit(60)
        )
    ).all()
    rating = await db.scalar(select(Rating).where(Rating.relationship_id == row.id))
    people = await names(db, {row.user_id, row.wakeel_id} | {t.actor_id for t in timeline})
    return {
        **dump(row),
        "people": people,
        "hiring_request": dump(request) if request else None,
        "cases": [dump(c, ("details",)) for c in cases],
        "agreements": [dump(a) for a in agreements],
        "documents": [file_dump(f) for f in files],
        "timeline": [
            {
                "id": t.id,
                "actor_id": t.actor_id,
                "summary": t.detail.rpartition("|")[0],
                "href": t.detail.rpartition("|")[2],
                "created_at": t.created_at,
            }
            for t in timeline
        ],
        "summary": await summary(db, row, user),
        "rating": dump(rating) if rating else None,
        "actions": relationship_actions(row, user),
        "can_rate": row.status == "ENDED" and user.id == row.user_id and rating is None,
    }


@router.post("/relationships/{identifier}/actions")
async def relationship_action(identifier: UUID, body: RelationshipAction, user: User, db: DB):
    row = await relationship(db, identifier, user, lock=True)
    if body.action not in relationship_actions(row, user):
        raise HTTPException(409, "This action is not available for the current engagement status.")
    other = peer_of(row, user)
    href = f"/workspaces/{row.id}"
    if body.action == "request_termination":
        row.termination_reason = need_reason(body.reason, 10)
        row.status, row.termination_requested_by, row.termination_requested_at = "TERMINATION_REQUESTED", user.id, now()
        row.termination_note = ""
        title = f"{user.name} asked to end your professional engagement"
        note = "Requested to end the engagement"
    elif body.action == "review_termination":
        row.status, row.termination_note = "UNDER_REVIEW", body.reason
        title = f"{user.name} is reviewing the request to end your engagement"
        note = "Started reviewing the termination request"
    elif body.action == "withdraw_termination":
        row.status, row.termination_requested_by, row.termination_requested_at = "ACTIVE", None, None
        title = f"{user.name} withdrew the request to end your engagement"
        note = "Withdrew the termination request"
    else:
        pending = (await summary(db, row, user))["open_cases"]
        if pending:
            # Never close ongoing legal matters silently: the parties resolve each one first.
            raise HTTPException(
                409,
                f"{len(pending)} case(s) are still open. Complete, close or cancel each one before ending the engagement.",
            )
        row.status, row.ended_at = "ENDED", now()
        if body.reason:
            row.termination_note = body.reason
        title = "Your professional engagement has ended"
        note = "Engagement ended"
    row.updated_at = now()
    activity(db, row.id, user, note, href)
    audit(db, user, "relationship_" + body.action, row.id, body.reason[:300])
    await notify(db, other, title, href)
    touch(db, row, f"/relationships/{row.id}")
    return dump(row)


@router.post("/relationships/{identifier}/documents", status_code=201)
async def relationship_document(
    identifier: UUID,
    file: UploadFile,
    user: User,
    db: DB,
    title: Annotated[str, Form(max_length=180)] = "",
    kind: Annotated[str, Form(max_length=40)] = "other",
    description: Annotated[str, Form(max_length=2000)] = "",
):
    row = await relationship(db, identifier, user, lock=True)
    if row.status == "ENDED":
        raise HTTPException(409, "This engagement has ended. Its documents are read-only.")
    stored = await store_file(db, file, user, title=title, kind=kind, description=description, relationship_id=row.id)
    activity(db, row.id, user, "Uploaded document: " + stored.title, f"/workspaces/{row.id}")
    audit(db, user, "document_uploaded", stored.id, "workspace")
    await notify(db, peer_of(row, user), f"{user.name} shared a document", f"/workspaces/{row.id}")
    touch(db, row, f"/relationships/{row.id}", "/documents")
    return file_dump(stored)


@router.post("/relationships/{identifier}/rating", status_code=201)
async def rate_relationship(identifier: UUID, body: RelationshipRatingIn, user: User, db: DB):
    row = await relationship(db, identifier, user, lock=True)
    if user.id != row.user_id:
        raise HTTPException(403, "Only the client can rate an engagement.")
    if row.status != "ENDED":
        raise HTTPException(409, "You can rate this engagement after it has ended.")
    if await db.scalar(select(Rating.id).where(Rating.relationship_id == row.id, Rating.author_id == user.id)):
        raise HTTPException(409, "You have already rated this engagement.")
    rating = Rating(
        author_id=user.id,
        subject_id=row.wakeel_id,
        relationship_id=row.id,
        overall=body.overall,
        scores=body.model_dump(exclude={"overall", "body"}),
        body=body.body,
    )
    db.add(rating)
    await db.flush()
    directory_changed(db)
    audit(db, user, "relationship_rated", row.id)
    await notify(db, row.wakeel_id, "A client rated your engagement", f"/workspaces/{row.id}")
    touch(db, row, f"/relationships/{row.id}", "/reviews")
    return dump(rating)
