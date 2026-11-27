"""Role dashboards, the document library and the calendar. One request per screen."""

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy import case as sql_case
from sqlalchemy.orm import defer

from app.platform.common import Offset, dump, file_dump, file_response
from app.platform.hiring import OPEN_CASES, OPEN_REQUESTS
from app.platform.models import (
    Account,
    Agreement,
    AgreementParty,
    Attachment,
    Audit,
    Case,
    CaseDeadline,
    CaseFee,
    CasePayment,
    Conversation,
    HiringRequest,
    Message,
    Notification,
    Relationship,
    now,
)
from app.platform.multiparty import visible_to
from app.platform.security import DB, User, audit

router = APIRouter(tags=["dashboard, documents and calendar"])


def mine(model, user):
    return or_(model.user_id == user.id, model.wakeel_id == user.id)


def count(*conditions, source):
    return select(func.count()).select_from(source).where(*conditions).scalar_subquery()


def my_agreements(user):
    service = and_(
        Agreement.kind == "service",
        Agreement.conversation_id.in_(select(Conversation.id).where(mine(Conversation, user))),
        Agreement.status != "DRAFT" if user.role == "user" else True,
    )
    return or_(service, visible_to(user))


@router.get("/dashboard")
async def dashboard(user: User, db: DB):
    my_cases = mine(Case, user)
    visible_case = and_(my_cases, or_(Case.status != "DRAFT", Case.proposed_by == user.id))
    agreements = my_agreements(user)
    # Proposals waiting for this account: the other party proposed the case or the fee.
    case_approvals = and_(my_cases, Case.status.in_(["SUBMITTED", "UNDER_DISCUSSION"]), Case.proposed_by != user.id)
    fee_approvals = and_(
        CaseFee.status == "PROPOSED",
        CaseFee.proposed_by != user.id,
        CaseFee.case_id.in_(select(Case.id).where(my_cases, Case.status.not_in(["SUBMITTED", "UNDER_DISCUSSION"]))),
    )
    payment_approvals = and_(
        CasePayment.status == "PENDING", CasePayment.case_id.in_(select(Case.id).where(Case.wakeel_id == user.id))
    )
    awaiting_action = (
        ["AWAITING_WAKEEL_ACTION", "ACCEPTED"]
        if user.role == "wakeel"
        else ["AWAITING_CLIENT_INFORMATION", "COMPLETION_REQUESTED"]
    )
    unsigned = and_(
        agreements,
        Agreement.status.in_(["SIGNING_PENDING", "PARTIALLY_SIGNED"]),
    )
    counts = (
        (
            await db.execute(
                select(
                    count(mine(Relationship, user), Relationship.status != "ENDED", source=Relationship).label(
                        "relationships"
                    ),
                    count(visible_case, Case.status.in_(OPEN_CASES), source=Case).label("active_cases"),
                    count(visible_case, Case.status.in_(["COMPLETED", "CLOSED"]), source=Case).label("completed_cases"),
                    count(my_cases, Case.status.in_(awaiting_action), source=Case).label("cases_awaiting_action"),
                    count(case_approvals, source=Case).label("case_approvals"),
                    count(fee_approvals, source=CaseFee).label("fee_approvals"),
                    count(payment_approvals, source=CasePayment).label("payment_approvals"),
                    count(
                        mine(HiringRequest, user), HiringRequest.status.in_(OPEN_REQUESTS), source=HiringRequest
                    ).label("hiring_requests"),
                    count(
                        agreements,
                        Agreement.status.in_(["SENT", "UNDER_REVIEW", "REVISION_REQUESTED", "NEGOTIATING", "APPROVED"]),
                        source=Agreement,
                    ).label("pending_agreements"),
                    count(unsigned, source=Agreement).label("signature_requests"),
                    count(
                        AgreementParty.account_id == user.id,
                        AgreementParty.invite_status == "PENDING",
                        AgreementParty.invite_token_hash.is_not(None),
                        source=AgreementParty,
                    ).label("invitations"),
                    count(Notification.user_id == user.id, Notification.read_at.is_(None), source=Notification).label(
                        "unread_notifications"
                    ),
                    count(
                        Message.conversation_id.in_(select(Conversation.id).where(mine(Conversation, user))),
                        Message.sender_id != user.id,
                        Message.read_at.is_(None),
                        Message.case_id.is_(None),
                        source=Message,
                    ).label("unread_messages"),
                )
            )
        )
        .mappings()
        .one()
    )
    result = dict(counts)
    result["pending_approvals"] = result["case_approvals"] + result["fee_approvals"] + result["payment_approvals"]

    today = now().date().isoformat()
    deadlines = (
        await db.execute(
            select(CaseDeadline, Case.title)
            .join(Case, Case.id == CaseDeadline.case_id)
            .where(my_cases, CaseDeadline.done.is_(False), CaseDeadline.due_on >= today)
            .order_by(CaseDeadline.due_on)
            .limit(6)
        )
    ).all()
    peer_id = sql_case((Conversation.user_id == user.id, Conversation.wakeel_id), else_=Conversation.user_id)
    newest = (
        select(Message.id)
        .where(Message.conversation_id == Conversation.id, Message.case_id.is_(None))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    threads = (
        await db.execute(
            select(
                Conversation.id, Conversation.updated_at, Account.name, Message.body, Message.sender_id, Message.read_at
            )
            .join(Account, Account.id == peer_id)
            .join(Message, Message.id == newest)
            .where(mine(Conversation, user))
            .order_by(Conversation.updated_at.desc())
            .limit(5)
        )
    ).all()
    activity = (
        await db.scalars(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .limit(8)
        )
    ).all()
    attention = (
        await db.scalars(
            select(Case)
            .where(or_(case_approvals, and_(my_cases, Case.status.in_(awaiting_action))))
            .order_by(Case.updated_at.desc())
            .limit(5)
        )
    ).all()
    return {
        "counts": result,
        "deadlines": [{**dump(d), "case_title": title} for d, title in deadlines],
        "messages": [
            {
                "conversation_id": t.id,
                "peer_name": t.name,
                "updated_at": t.updated_at,
                "preview": (t.body or "")[:120],
                "unread": t.sender_id != user.id and t.read_at is None,
            }
            for t in threads
        ],
        "activity": [dump(n) for n in activity],
        "attention": [
            {"id": c.id, "title": c.title, "status": c.status, "updated_at": c.updated_at} for c in attention
        ],
    }


# ------------------------------------------------------------------ documents
async def accessible(db, file, user):
    """Authorise through the document's parent. Every download re-checks membership."""
    if file.case_id:
        parent = await db.scalar(select(Case).where(Case.id == file.case_id, mine(Case, user)))
    elif file.relationship_id:
        parent = await db.scalar(
            select(Relationship).where(Relationship.id == file.relationship_id, mine(Relationship, user))
        )
    else:
        parent = await db.scalar(
            select(HiringRequest).where(HiringRequest.id == file.hiring_request_id, mine(HiringRequest, user))
        )
    return parent is not None


@router.get("/documents")
async def documents(user: User, db: DB, offset: Offset = 0):
    query = (
        select(Attachment, Case.title)
        .options(defer(Attachment.content))
        .outerjoin(Case, Case.id == Attachment.case_id)
        .where(
            or_(
                Attachment.case_id.in_(select(Case.id).where(mine(Case, user))),
                Attachment.relationship_id.in_(select(Relationship.id).where(mine(Relationship, user))),
                Attachment.hiring_request_id.in_(select(HiringRequest.id).where(mine(HiringRequest, user))),
            )
        )
        .order_by(Attachment.created_at.desc())
        .offset(offset)
        .limit(200)
    )
    rows = (await db.execute(query)).all()
    people = dict(
        (
            await db.execute(
                select(Account.id, Account.name).where(Account.id.in_({f.uploader_id for f, _ in rows} or [""]))
            )
        ).all()
    )
    return [
        {
            **file_dump(file),
            "case_title": title,
            "uploader_name": people.get(file.uploader_id, ""),
            "context": "case" if file.case_id else "workspace" if file.relationship_id else "hiring request",
        }
        for file, title in rows
    ]


@router.get("/documents/{identifier}")
async def download(identifier: UUID, user: User, db: DB):
    file = await db.get(Attachment, str(identifier))
    if not file or not await accessible(db, file, user):
        raise HTTPException(404, "This document could not be found.")
    audit(db, user, "document_downloaded", file.id)
    return file_response(file)


# ------------------------------------------------------------------- calendar
@router.get("/calendar")
async def calendar(user: User, db: DB):
    """Dated items across the account's cases, from 30 days ago onwards."""
    start = (now() - timedelta(days=30)).date().isoformat()
    my_cases = mine(Case, user)
    deadlines = (
        await db.execute(
            select(CaseDeadline, Case.title)
            .join(Case, Case.id == CaseDeadline.case_id)
            .where(my_cases, CaseDeadline.due_on >= start)
            .order_by(CaseDeadline.due_on)
            .limit(300)
        )
    ).all()
    items = [
        {
            "id": d.id,
            "case_id": d.case_id,
            "case_title": title,
            "title": d.title,
            "kind": d.kind,
            "date": d.due_on,
            "done": d.done,
        }
        for d, title in deadlines
    ]
    cases = (
        await db.scalars(
            select(Case)
            .where(
                my_cases,
                Case.status.in_(OPEN_CASES),
                or_(Case.important_date >= start, Case.expected_completion >= start),
            )
            .limit(200)
        )
    ).all()
    for row in cases:
        for kind, value, title in (
            ("hearing", row.important_date, "Important date"),
            ("completion", row.expected_completion, "Expected completion"),
        ):
            if value and value >= start:
                items.append(
                    {
                        "id": f"{row.id}:{kind}",
                        "case_id": row.id,
                        "case_title": row.title,
                        "title": title,
                        "kind": kind,
                        "date": value,
                        "done": False,
                    }
                )
    return sorted(items, key=lambda item: item["date"])


@router.get("/activity")
async def recent_activity(user: User, db: DB, offset: Offset = 0):
    """The account's own audited actions (security-relevant history for Settings)."""
    rows = (
        await db.scalars(
            select(Audit)
            .where(
                Audit.actor_id == user.id,
                Audit.action.in_(
                    ["login", "password_changed", "password_reset", "password_reset_requested", "profile_updated"]
                ),
            )
            .order_by(Audit.created_at.desc())
            .offset(offset)
            .limit(20)
        )
    ).all()
    return [{"id": r.id, "action": r.action, "created_at": r.created_at} for r in rows]
