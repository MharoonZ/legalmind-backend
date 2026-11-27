"""Cases: independent legal matters with their own terms, fees, thread and history.

Cases opened inside an engagement follow a proposal workflow: one party
proposes, the other accepts, rejects or asks for changes. Legacy cases
(no relationship) keep their original status workflow.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import defer

from app.platform.common import (
    Offset,
    activity,
    agreement,
    dump,
    file_dump,
    file_response,
    need_reason,
    store_file,
    verified_wakeel,
)
from app.platform.events import invalidate, queue_event
from app.platform.models import (
    Account,
    Agreement,
    Attachment,
    Case,
    CaseAgreement,
    CaseDeadline,
    CaseFee,
    CasePayment,
    CaseUpdate,
    Conversation,
    Message,
    Rating,
    Relationship,
    now,
)
from app.platform.schemas import (
    CaseAction,
    CaseAssignment,
    CaseIn,
    CaseLink,
    CaseNote,
    CaseRatingIn,
    CaseRevision,
    DeadlineIn,
    DeadlineState,
    Decision,
    FeeIn,
    PaymentIn,
    SendMessage,
)
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
    parties = [uid for uid in (row.user_id, row.wakeel_id) if uid]
    row.updated_at = now()
    invalidate(db, parties, "/cases", f"/cases/{row.id}", "/agreements", "/dashboard", "/calendar")
    if row.relationship_id:
        invalidate(db, parties, f"/relationships/{row.relationship_id}", "/relationships")
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


SORTS = {
    "updated": Case.updated_at.desc(),
    "created": Case.created_at.desc(),
    "title": Case.title,
    "priority": Case.priority,
}


@router.get("")
async def cases(
    user: User,
    db: DB,
    offset: Offset = 0,
    q: Annotated[str, Query(max_length=120)] = "",
    status: Annotated[str, Query(max_length=32)] = "",
    relationship_id: UUID | None = None,
    sort: Annotated[str, Query(max_length=12)] = "updated",
):
    query = select(Case).where(or_(Case.user_id == user.id, Case.wakeel_id == user.id))
    if q:
        query = query.where(
            or_(Case.title.icontains(q, autoescape=True), Case.reference_number.icontains(q, autoescape=True))
        )
    if status == "open":
        query = query.where(Case.status.not_in(TERMINAL | {"DRAFT"}))
    elif status == "closed":
        query = query.where(Case.status.in_(TERMINAL))
    elif status:
        query = query.where(Case.status == status.upper())
    if relationship_id:
        query = query.where(Case.relationship_id == str(relationship_id))
    # A draft is private to its author until it is submitted.
    query = query.where(or_(Case.status != "DRAFT", Case.proposed_by == user.id, Case.proposed_by.is_(None)))
    rows = (
        await db.scalars(query.order_by(SORTS.get(sort, SORTS["updated"]), Case.id).offset(offset).limit(200))
    ).all()
    ids = {uid for c in rows for uid in (c.user_id, c.wakeel_id) if uid and uid != user.id}
    people = dict((await db.execute(select(Account.id, Account.name).where(Account.id.in_(ids or [""])))).all())
    return [
        {
            **dump(c, ("details", "legal_issue")),
            "peer_name": people.get(c.wakeel_id if c.user_id == user.id else c.user_id, ""),
        }
        for c in rows
    ]


def apply_fields(row, body):
    for name in (
        "title",
        "description",
        "category",
        "subcategory",
        "legal_issue",
        "priority",
        "jurisdiction",
        "court",
        "reference_number",
        "opposing_party",
        "fee_type",
        "currency",
    ):
        setattr(row, name, getattr(body, name))
    row.proposed_fee = body.proposed_fee
    row.proposed_start = body.proposed_start.isoformat() if body.proposed_start else None
    row.expected_completion = body.expected_completion.isoformat() if body.expected_completion else None
    row.important_date = body.important_date.isoformat() if body.important_date else None
    row.details = {
        **body.details,
        **{
            key: str(value)
            for key, value in (
                ("advance_payment", body.advance_payment),
                ("payment_schedule", body.payment_schedule),
                ("expenses", body.expenses),
                ("payment_notes", body.payment_notes),
            )
            if value not in (None, "")
        },
    }


def history(db, row, user, note, before=None, kind="update"):
    db.add(
        CaseUpdate(
            case_id=row.id,
            actor_id=user.id,
            note=note,
            status=row.status,
            important_date=row.important_date,
            kind=kind,
            from_status=before,
        )
    )


async def propose_fee(db, row, user, body_or_amount, **fields):
    """Supersede any open proposal; the agreed fee only changes on acceptance."""
    open_fees = (await db.scalars(select(CaseFee).where(CaseFee.case_id == row.id, CaseFee.status == "PROPOSED"))).all()
    for fee in open_fees:
        fee.status = "SUPERSEDED"
    fee = CaseFee(case_id=row.id, proposed_by=user.id, amount=body_or_amount, **fields)
    db.add(fee)
    row.proposed_fee, row.fee_type, row.currency = (
        fee.amount,
        fee.fee_type or row.fee_type,
        fee.currency or row.currency,
    )
    await db.flush()
    return fee


async def create_proposal(body, user, db):
    engagement = await db.scalar(
        select(Relationship)
        .where(
            Relationship.id == str(body.relationship_id),
            or_(Relationship.user_id == user.id, Relationship.wakeel_id == user.id),
        )
        .with_for_update()
    )
    if not engagement:
        raise HTTPException(404, "This workspace could not be found.")
    if engagement.status != "ACTIVE":
        raise HTTPException(409, "New cases can only be opened in an active engagement.")
    row = Case(
        user_id=engagement.user_id,
        wakeel_id=engagement.wakeel_id,
        relationship_id=engagement.id,
        proposed_by=user.id,
        title=body.title,
        description=body.description,
        status="DRAFT" if body.draft else "SUBMITTED",
    )
    apply_fields(row, body)
    db.add(row)
    await db.flush()
    if body.proposed_fee is not None:
        await propose_fee(
            db,
            row,
            user,
            body.proposed_fee,
            fee_type=body.fee_type,
            currency=body.currency,
            advance=body.advance_payment,
            schedule=body.payment_schedule,
            expenses=body.expenses,
            note=body.payment_notes,
        )
    history(db, row, user, "Case draft created" if body.draft else "Case proposed", None, "status")
    audit(db, user, "case_created", row.id)
    if body.draft:
        invalidate(db, [user.id], "/cases", "/dashboard")
    else:
        activity(db, engagement.id, user, "Proposed case: " + row.title, f"/cases/{row.id}")
        await changed(db, row, user, f"{user.name} proposed a case: {row.title}")
    return dump(row)


@router.post("", status_code=201)
async def create(body: CaseIn, user: User, db: DB):
    if body.relationship_id:
        return await create_proposal(body, user, db)
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
    people_ids = {uid for uid in (row.user_id, row.wakeel_id, row.proposed_by) if uid} | {u.actor_id for u in updates}
    people = (await db.scalars(select(Account).where(Account.id.in_(people_ids)))).all()
    thread = (
        await db.scalar(
            select(Conversation).where(Conversation.user_id == row.user_id, Conversation.wakeel_id == row.wakeel_id)
        )
        if row.wakeel_id
        else None
    )
    if row.status == "DRAFT" and row.proposed_by and row.proposed_by != user.id:
        raise HTTPException(404, "Case not found.")
    fees = (await db.scalars(select(CaseFee).where(CaseFee.case_id == row.id).order_by(CaseFee.created_at))).all()
    payments = (
        await db.scalars(select(CasePayment).where(CasePayment.case_id == row.id).order_by(CasePayment.created_at))
    ).all()
    deadlines = (
        await db.scalars(select(CaseDeadline).where(CaseDeadline.case_id == row.id).order_by(CaseDeadline.due_on))
    ).all()
    rating = await db.scalar(select(Rating).where(Rating.case_id == row.id))
    engagement = await db.get(Relationship, row.relationship_id) if row.relationship_id else None
    paid = sum(float(p.amount) for p in payments if p.status == "CONFIRMED")
    return {
        **dump(row),
        "conversation_id": thread.id if thread else None,
        "allowed_statuses": sorted(allowed_statuses(row, user)),
        "actions": case_actions(row, user, engagement),
        "updates": [dump(u) for u in updates],
        "attachments": [file_dump(a) for a in attachments],
        "agreements": [dump(a) for a in linked],
        "people": {p.id: p.name for p in people},
        "fees": [dump(f) for f in fees],
        "payments": [dump(p) for p in payments],
        "deadlines": [dump(d) for d in deadlines],
        "paid": paid,
        "rating": dump(rating) if rating else None,
        "can_rate": row.status in {"COMPLETED", "CLOSED"} and user.id == row.user_id and rating is None,
        "relationship_status": engagement.status if engagement else None,
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


# Engagement cases: negotiation -> work -> completion. Work statuses are managed by the Wakeel.
NEGOTIATION = {"SUBMITTED", "UNDER_DISCUSSION", "CHANGES_REQUESTED"}
WORK = {
    "ACTIVE",
    "IN_PROGRESS",
    "ON_HOLD",
    "AWAITING_CLIENT_INFORMATION",
    "AWAITING_WAKEEL_ACTION",
    "AWAITING_HEARING",
    "HEARING_COMPLETED",
    "AWAITING_DECISION",
}
TERMINAL = {"REJECTED", "CANCELLED", "CLOSED", "DISMISSED", "RESOLVED", "COMPLETED"}
WORK_TRANSITIONS = {
    "ACCEPTED": {"ACTIVE"},
    **{status: (WORK - {status, "ACTIVE"}) for status in WORK},
}


def allowed_statuses(row, user):
    if not row.relationship_id:
        return TRANSITIONS.get(row.status, set())
    return WORK_TRANSITIONS.get(row.status, set()) if user.id == row.wakeel_id else set()


def case_actions(row, user, engagement=None):
    """Actions the signed-in participant may take now; the server re-checks every one."""
    if not row.relationship_id:
        return []
    proposer = row.proposed_by == user.id
    wakeel = user.id == row.wakeel_id
    status = row.status
    actions = []
    if status == "DRAFT":
        actions = ["submit", "revise", "cancel"] if proposer else []
    elif status in {"SUBMITTED", "UNDER_DISCUSSION"}:
        if proposer:
            actions = ["revise", "cancel"]
        else:
            actions = ["accept", "request_changes", "reject", "revise"] + (["discuss"] if status == "SUBMITTED" else [])
        actions.append("propose_fee")
    elif status == "CHANGES_REQUESTED":
        actions = ["revise", "cancel"] if proposer else []
    elif status == "ACCEPTED" or status in WORK:
        actions = ["propose_fee", "record_payment", "add_deadline", "update"]
        if wakeel and status in WORK:
            actions.append("request_completion")
        if engagement and engagement.status != "ACTIVE":
            actions.append("close")
    elif status == "COMPLETION_REQUESTED":
        actions = ["record_payment"] + (["confirm_completion", "raise_concern"] if not wakeel else [])
    elif status == "COMPLETED":
        actions = ["close", "record_payment"]
    return actions


def require_action(row, user, action, engagement=None):
    if action not in case_actions(row, user, engagement):
        raise HTTPException(409, "This action is not available for the current case status.")


async def engagement_of(db, row):
    return await db.get(Relationship, row.relationship_id) if row.relationship_id else None


def other_of(row, user):
    return row.wakeel_id if user.id == row.user_id else row.user_id


@router.post("/{identifier}/actions")
async def act(identifier: UUID, body: CaseAction, user: User, db: DB):
    row = await owned(db, identifier, user)
    engagement = await engagement_of(db, row)
    require_action(row, user, body.action, engagement)
    before, action = row.status, body.action
    reason = body.reason.strip()
    if action == "submit":
        row.status, note, title = "SUBMITTED", "Case proposed", f"{user.name} proposed a case: {row.title}"
        activity(db, row.relationship_id, user, "Proposed case: " + row.title, f"/cases/{row.id}")
    elif action == "discuss":
        row.status, note, title = (
            "UNDER_DISCUSSION",
            reason or "Opened for discussion",
            f"{user.name} wants to discuss: {row.title}",
        )
    elif action == "request_changes":
        row.status, note = "CHANGES_REQUESTED", "Changes requested: " + need_reason(reason)
        title = f"{user.name} requested changes to: {row.title}"
    elif action == "reject":
        row.status, note = "REJECTED", "Rejected: " + need_reason(reason)
        title = f"{user.name} rejected the case: {row.title}"
    elif action == "cancel":
        row.status, note, title = (
            "CANCELLED",
            "Cancelled" + (": " + reason if reason else ""),
            f"Case cancelled: {row.title}",
        )
    elif action == "accept":
        fee = await db.scalar(select(CaseFee).where(CaseFee.case_id == row.id, CaseFee.status == "PROPOSED"))
        if fee and fee.proposed_by == user.id:
            raise HTTPException(409, "The other party must respond to your fee proposal before the case is accepted.")
        if fee:
            fee.status, fee.decided_by, fee.decided_at = "ACCEPTED", user.id, now()
            row.agreed_fee, row.fee_type, row.currency = fee.amount, fee.fee_type, fee.currency
        row.status, row.agreed_start = "ACCEPTED", row.proposed_start
        note, title = "Case accepted" + (": " + reason if reason else ""), f"{user.name} accepted the case: {row.title}"
        activity(db, row.relationship_id, user, "Accepted case: " + row.title, f"/cases/{row.id}")
    elif action == "request_completion":
        row.completion_summary = need_reason(reason, 10)
        row.status, note = "COMPLETION_REQUESTED", "Completion requested: " + row.completion_summary
        title = f"{user.name} marked the case as complete: {row.title}"
    elif action == "confirm_completion":
        row.status, row.completed_at = "COMPLETED", now()
        note, title = "Client confirmed completion", f"{user.name} confirmed completion: {row.title}"
        activity(db, row.relationship_id, user, "Completed case: " + row.title, f"/cases/{row.id}")
    elif action == "raise_concern":
        row.status, note = "IN_PROGRESS", "Concern raised: " + need_reason(reason, 10)
        title = f"{user.name} raised a concern about: {row.title}"
    else:  # close
        if before != "COMPLETED":
            # Only reachable while the engagement is ending: record why the matter stops here.
            reason = need_reason(reason, 10)
        row.status, row.closed_at = "CLOSED", now()
        note, title = "Case closed" + (": " + reason if reason else ""), f"Case closed: {row.title}"
        activity(db, row.relationship_id, user, "Closed case: " + row.title, f"/cases/{row.id}")
    history(db, row, user, note, before, "status")
    audit(db, user, "case_" + action, row.id, reason[:300])
    await changed(db, row, user, title)
    return dump(row)


@router.post("/{identifier}/revise")
async def revise(identifier: UUID, body: CaseRevision, user: User, db: DB):
    row = await owned(db, identifier, user)
    require_action(row, user, "revise")
    before = row.status
    previous_fee = row.proposed_fee
    apply_fields(row, body)
    if body.proposed_fee is not None and body.proposed_fee != previous_fee:
        await propose_fee(
            db,
            row,
            user,
            body.proposed_fee,
            fee_type=body.fee_type,
            currency=body.currency,
            advance=body.advance_payment,
            schedule=body.payment_schedule,
            expenses=body.expenses,
            note=body.payment_notes,
        )
    if before != "DRAFT":
        # The reviser becomes the proposer, so the other party must approve the new terms.
        row.status, row.proposed_by = "SUBMITTED", user.id
        history(db, row, user, "Case terms revised" + (": " + body.reason if body.reason else ""), before, "status")
        await changed(db, row, user, f"{user.name} revised the case: {row.title}")
    else:
        row.updated_at = now()
        invalidate(db, [user.id], "/cases", f"/cases/{row.id}")
    audit(db, user, "case_revised", row.id)
    return dump(row)


@router.post("/{identifier}/updates")
async def update(identifier: UUID, body: CaseNote, user: User, db: DB):
    row = await owned(db, identifier, user)
    date_value = body.important_date.isoformat() if body.important_date else None
    before = row.status
    if row.relationship_id and row.status not in WORK | {"ACCEPTED"}:
        raise HTTPException(409, "Updates can be added once the case has been accepted and until it is completed.")
    if (body.status != row.status or date_value != row.important_date) and user.id != (row.wakeel_id or row.user_id):
        raise HTTPException(403, "The assigned Wakeel manages case status and hearing dates.")
    if body.status != row.status and body.status not in allowed_statuses(row, user):
        raise HTTPException(409, "This case status transition is unavailable.")
    if body.status == "ACTIVE" and row.relationship_id and row.agreed_fee is None:
        raise HTTPException(409, "Agree the fee with your client before the case becomes active.")
    row.status, row.important_date = body.status, date_value
    history(
        db,
        row,
        user,
        body.note,
        before if before != row.status else None,
        "status" if before != row.status else "update",
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
async def upload(
    identifier: UUID,
    file: UploadFile,
    user: User,
    db: DB,
    title: Annotated[str, Form(max_length=180)] = "",
    kind: Annotated[str, Form(max_length=40)] = "other",
    description: Annotated[str, Form(max_length=2000)] = "",
):
    row = await owned(db, identifier, user)
    if row.status in {"REJECTED", "CANCELLED", "CLOSED"}:
        raise HTTPException(409, "This case is closed. Its documents are read-only.")
    attachment = await store_file(db, file, user, title=title, kind=kind, description=description, case_id=row.id)
    history(db, row, user, "Document uploaded: " + attachment.title, None, "document")
    audit(db, user, "attachment_uploaded", attachment.id)
    invalidate(db, [uid for uid in (row.user_id, row.wakeel_id) if uid], "/documents")
    await changed(db, row, user, "New case document")
    return file_dump(attachment)


@router.get("/{identifier}/attachments/{attachment_id}")
async def download(identifier: UUID, attachment_id: UUID, user: User, db: DB):
    row = await owned(db, identifier, user)
    file = await db.get(Attachment, str(attachment_id))
    if not file or file.case_id != row.id:
        raise HTTPException(404, "Attachment not found.")
    audit(db, user, "document_downloaded", file.id)
    return file_response(file)


# ------------------------------------------------------------- fees, payments
def parties(row):
    return [uid for uid in (row.user_id, row.wakeel_id) if uid]


@router.post("/{identifier}/fees", status_code=201)
async def fee_proposal(identifier: UUID, body: FeeIn, user: User, db: DB):
    row = await owned(db, identifier, user)
    require_action(row, user, "propose_fee", await engagement_of(db, row))
    fee = await propose_fee(db, row, user, body.amount, **body.model_dump(exclude={"amount"}))
    history(db, row, user, f"Fee proposed: {fee.currency} {fee.amount:,.2f} ({fee.fee_type.lower()})", None, "fee")
    audit(db, user, "case_fee_proposed", row.id, f"{fee.currency} {fee.amount}")
    await changed(db, row, user, f"{user.name} proposed a fee for: {row.title}")
    return dump(fee)


@router.post("/{identifier}/fees/{fee_id}/decision")
async def fee_decision(identifier: UUID, fee_id: UUID, body: Decision, user: User, db: DB):
    row = await owned(db, identifier, user)
    fee = await db.scalar(select(CaseFee).where(CaseFee.id == str(fee_id), CaseFee.case_id == row.id).with_for_update())
    if not fee:
        raise HTTPException(404, "Fee proposal not found.")
    if fee.status != "PROPOSED" or row.status in TERMINAL | {"DRAFT"}:
        raise HTTPException(409, "This fee proposal is no longer open.")
    if fee.proposed_by == user.id:
        raise HTTPException(403, "The other party decides on your fee proposal.")
    if body.action == "accept":
        fee.status = "ACCEPTED"
        row.agreed_fee, row.fee_type, row.currency = fee.amount, fee.fee_type, fee.currency
        note = f"Fee agreed: {fee.currency} {fee.amount:,.2f}"
    elif body.action == "reject":
        fee.status = "REJECTED"
        row.proposed_fee = row.agreed_fee
        note = "Fee proposal declined" + (": " + body.note if body.note else "")
    else:
        raise HTTPException(422, "Choose accept or reject.")
    fee.decided_by, fee.decided_at, fee.decision_note = user.id, now(), body.note
    await refresh_payment_status(db, row)
    history(db, row, user, note, None, "fee")
    audit(db, user, "case_fee_" + body.action, row.id, f"{fee.currency} {fee.amount}")
    await changed(db, row, user, f"{note} — {row.title}")
    return dump(fee)


async def refresh_payment_status(db, row):
    await db.flush()
    paid = await db.scalar(
        select(func.coalesce(func.sum(CasePayment.amount), 0)).where(
            CasePayment.case_id == row.id, CasePayment.status == "CONFIRMED"
        )
    )
    paid = float(paid or 0)
    agreed = float(row.agreed_fee) if row.agreed_fee is not None else None
    row.payment_status = "UNPAID" if paid <= 0 else "PAID" if agreed is not None and paid >= agreed else "PARTIAL"


@router.post("/{identifier}/payments", status_code=201)
async def record_payment(identifier: UUID, body: PaymentIn, user: User, db: DB):
    row = await owned(db, identifier, user)
    require_action(row, user, "record_payment", await engagement_of(db, row))
    # Money received is only counted once the Wakeel, as payee, confirms it.
    payee = user.id == row.wakeel_id
    payment = CasePayment(
        case_id=row.id,
        recorded_by=user.id,
        status="CONFIRMED" if payee else "PENDING",
        decided_by=user.id if payee else None,
        decided_at=now() if payee else None,
        **body.model_dump(),
    )
    db.add(payment)
    await refresh_payment_status(db, row)
    label = "Payment received" if payee else "Payment reported, awaiting confirmation"
    history(db, row, user, f"{label}: {row.currency} {payment.amount:,.2f}", None, "payment")
    audit(db, user, "case_payment_recorded", row.id, f"{row.currency} {payment.amount}")
    await changed(db, row, user, f"{label} — {row.title}")
    return dump(payment)


@router.post("/{identifier}/payments/{payment_id}/decision")
async def payment_decision(identifier: UUID, payment_id: UUID, body: Decision, user: User, db: DB):
    row = await owned(db, identifier, user)
    payment = await db.scalar(
        select(CasePayment).where(CasePayment.id == str(payment_id), CasePayment.case_id == row.id).with_for_update()
    )
    if not payment:
        raise HTTPException(404, "Payment not found.")
    if user.id != row.wakeel_id:
        raise HTTPException(403, "Only the Wakeel receiving the payment can confirm it.")
    if payment.status != "PENDING":
        raise HTTPException(409, "This payment has already been reviewed.")
    if body.action not in {"confirm", "reject"}:
        raise HTTPException(422, "Choose confirm or reject.")
    payment.status = "CONFIRMED" if body.action == "confirm" else "REJECTED"
    payment.decided_by, payment.decided_at = user.id, now()
    await refresh_payment_status(db, row)
    note = f"Payment {payment.status.lower()}: {row.currency} {payment.amount:,.2f}"
    history(db, row, user, note, None, "payment")
    audit(db, user, "case_payment_" + body.action, row.id)
    await changed(db, row, user, f"{note} — {row.title}")
    return dump(payment)


# ------------------------------------------------------------------ deadlines
@router.post("/{identifier}/deadlines", status_code=201)
async def add_deadline(identifier: UUID, body: DeadlineIn, user: User, db: DB):
    row = await owned(db, identifier, user)
    if row.relationship_id:
        require_action(row, user, "add_deadline", await engagement_of(db, row))
    deadline = CaseDeadline(
        case_id=row.id, created_by=user.id, title=body.title, kind=body.kind, due_on=body.due_on.isoformat()
    )
    db.add(deadline)
    history(db, row, user, f"{body.kind.capitalize()} scheduled for {deadline.due_on}: {body.title}", None, "deadline")
    await db.flush()
    await changed(db, row, user, f"New {body.kind} date — {row.title}")
    return dump(deadline)


@router.post("/{identifier}/deadlines/{deadline_id}")
async def deadline_state(identifier: UUID, deadline_id: UUID, body: DeadlineState, user: User, db: DB):
    row = await owned(db, identifier, user)
    deadline = await db.get(CaseDeadline, str(deadline_id))
    if not deadline or deadline.case_id != row.id:
        raise HTTPException(404, "Deadline not found.")
    deadline.done = body.done
    row.updated_at = now()
    invalidate(db, parties(row), f"/cases/{row.id}", "/calendar", "/dashboard")
    return dump(deadline)


# ------------------------------------------------------------ case discussion
async def thread_of(db, row):
    if not row.wakeel_id:
        raise HTTPException(409, "Case discussion opens once a Wakeel is assigned.")
    thread = await db.scalar(
        select(Conversation).where(Conversation.user_id == row.user_id, Conversation.wakeel_id == row.wakeel_id)
    )
    if not thread:
        thread = Conversation(user_id=row.user_id, wakeel_id=row.wakeel_id)
        db.add(thread)
        await db.flush()
    return thread


@router.get("/{identifier}/messages")
async def case_messages(identifier: UUID, user: User, db: DB, before: UUID | None = None):
    row = await owned(db, identifier, user)
    query = select(Message).where(Message.case_id == row.id)
    if before:
        cursor = await db.get(Message, str(before))
        if not cursor or cursor.case_id != row.id:
            raise HTTPException(404, "Message not found.")
        query = query.where(
            or_(
                Message.created_at < cursor.created_at,
                and_(Message.created_at == cursor.created_at, Message.id < cursor.id),
            )
        )
    rows = (await db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(100))).all()
    return {"case_id": row.id, "messages": [dump(m) for m in reversed(rows)], "has_more": len(rows) == 100}


@router.post("/{identifier}/messages", status_code=201)
async def send_case_message(identifier: UUID, body: SendMessage, user: User, db: DB):
    row = await owned(db, identifier, user)
    if row.status == "DRAFT":
        raise HTTPException(409, "Submit the case before starting its discussion.")
    thread = await thread_of(db, row)
    existing = await db.scalar(
        select(Message).where(
            Message.conversation_id == thread.id,
            Message.sender_id == user.id,
            Message.request_id == str(body.request_id),
        )
    )
    if existing:
        if existing.body != body.body or existing.case_id != row.id:
            raise HTTPException(409, "This request identifier belongs to another message.")
        return dump(existing)
    # Case messages are read in the case thread and never counted as unread general chat.
    message = Message(
        conversation_id=thread.id,
        sender_id=user.id,
        request_id=str(body.request_id),
        body=body.body,
        case_id=row.id,
        read_at=now(),
    )
    db.add(message)
    await db.flush()
    recipients = parties(row)
    queue_event(db, recipients, "case.message", case_id=row.id, message_id=message.id)
    invalidate(db, recipients, f"/cases/{row.id}/messages")
    await notify(db, other_of(row, user), f"New message in case: {row.title}", f"/cases/{row.id}")
    return dump(message)


# -------------------------------------------------------------------- ratings
@router.post("/{identifier}/rating", status_code=201)
async def rate_case(identifier: UUID, body: CaseRatingIn, user: User, db: DB):
    from app.platform.events import directory_changed

    row = await owned(db, identifier, user)
    if user.id != row.user_id:
        raise HTTPException(403, "Only the client can rate a case.")
    if row.status not in {"COMPLETED", "CLOSED"} or not row.wakeel_id:
        raise HTTPException(409, "You can rate this case after it has been completed.")
    if await db.scalar(select(Rating.id).where(Rating.case_id == row.id, Rating.author_id == user.id)):
        raise HTTPException(409, "You have already rated this case.")
    rating = Rating(
        author_id=user.id,
        subject_id=row.wakeel_id,
        case_id=row.id,
        overall=body.overall,
        scores=body.model_dump(exclude={"overall", "body"}),
        body=body.body,
    )
    db.add(rating)
    await db.flush()
    directory_changed(db)
    audit(db, user, "case_rated", row.id)
    invalidate(db, parties(row), f"/cases/{row.id}", "/reviews")
    await notify(db, row.wakeel_id, f"Your client rated the case: {row.title}", f"/cases/{row.id}")
    return dump(rating)
