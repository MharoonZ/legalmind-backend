"""Versioned service agreements. All mutations lock the agreement and check its revision."""

import hashlib
import json
from uuid import UUID

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, delete, or_, select

from app.platform.common import Offset, agreement, conversation, dump, other_party, verified_wakeel
from app.platform.models import (
    Account,
    AgreementDraft,
    Case,
    CaseAgreement,
    Acceptance,
    Agreement,
    AgreementEvent,
    AgreementVersion,
    Conversation,
    Preparation,
    now,
)
from app.platform.schemas import AgreementAction, CreateAgreement, Terms, parse
from app.platform.security import DB, User, audit, notify

router = APIRouter(prefix="/agreements", tags=["service agreements"])


def version_digest(terms):
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def add_version(db, item, user, terms):
    data = terms.model_dump(mode="json")
    db.add(
        AgreementVersion(
            agreement_id=item.id,
            number=item.current_version,
            author_id=user.id,
            terms=data,
            digest=version_digest(data),
        )
    )
    item.title = terms.title


def event(db, item, user, action, before, reason="", version=None):
    db.add(
        AgreementEvent(
            agreement_id=item.id,
            actor_id=user.id,
            action=action,
            from_status=before,
            to_status=item.status,
            version=item.current_version if version is None else version,
            reason=reason,
        )
    )
    audit(db, user, "agreement_" + action, item.id)
    item.updated_at = now()


async def service_or_multiparty(db, identifier, user, lock=False):
    """Load a service agreement; (None, None) means the id may be a multi-party agreement."""
    try:
        return await agreement(db, identifier, user, lock=lock)
    except HTTPException:
        if await db.scalar(select(Agreement.kind).where(Agreement.id == str(identifier))) == "multi_party":
            return None, None
        raise


@router.get("")
async def agreements(
    user: User,
    db: DB,
    offset: Offset = 0,
    q: Annotated[str, Query(max_length=120)] = "",
    status: Annotated[str, Query(max_length=32)] = "",
    kind: Annotated[str, Query(max_length=16)] = "",
):
    from app.platform.multiparty import visible_to

    service = and_(
        Agreement.kind == "service",
        or_(Conversation.user_id == user.id, Conversation.wakeel_id == user.id),
        # Clients never see a Wakeel's private draft.
        Agreement.status != "DRAFT" if user.role == "user" else True,
    )
    query = select(Agreement).outerjoin(Conversation).where(or_(service, visible_to(user)))
    if q:
        query = query.where(
            or_(Agreement.title.icontains(q, autoescape=True), Agreement.reference.icontains(q, autoescape=True))
        )
    if status:
        query = query.where(Agreement.status == status.upper())
    if kind in {"service", "multi_party"}:
        query = query.where(Agreement.kind == kind)
    return [
        dump(a) for a in (await db.scalars(query.order_by(Agreement.updated_at.desc()).offset(offset).limit(200))).all()
    ]


@router.post("", status_code=201)
async def create(body: CreateAgreement, user: User, db: DB):
    await verified_wakeel(db, user)
    thread = await conversation(db, body.conversation_id, user, lock=True)
    # A proposal follows an actual conversation, including a preparation request.
    from app.platform.models import Message

    if not await db.scalar(select(Message.id).where(Message.conversation_id == thread.id).limit(1)):
        raise HTTPException(409, "Discuss the requirements in this conversation before preparing a contract.")
    preparation = None
    if body.preparation_id:
        preparation = await db.get(Preparation, str(body.preparation_id))
        if not preparation or preparation.conversation_id != thread.id:
            raise HTTPException(404, "Preparation request not found.")
        if preparation.status not in {"PREPARATION_REQUESTED", "ACCEPTED"}:
            raise HTTPException(409, "This request already has an agreement.")
        preparation.status = "DRAFT"
    from app.platform.models import Relationship

    workspace = await db.scalar(
        select(Relationship.id).where(Relationship.conversation_id == thread.id, Relationship.status != "ENDED")
    )
    item = Agreement(
        conversation_id=thread.id,
        preparation_id=preparation.id if preparation else None,
        title=body.terms.title,
        creator_id=user.id,
        relationship_id=workspace,
        category="Legal service agreement",
    )
    db.add(item)
    await db.flush()
    add_version(db, item, user, body.terms)
    event(db, item, user, "create", "PREPARATION_REQUESTED" if preparation else "DRAFT")
    await db.flush()
    if body.case_id:
        from app.platform.cases import owned, link_agreement

        case = await owned(db, body.case_id, user)
        await link_agreement(db, case, item, thread, user)
    await db.execute(delete(AgreementDraft).where(AgreementDraft.conversation_id == thread.id))
    from app.platform.mail import enqueue
    from app.platform.events import invalidate

    await enqueue(db, user.id, "agreements", "Agreement draft created: " + item.title, f"/agreements/{item.id}")
    invalidate(db, [user.id], "/agreements", "/agreement-drafts", "/preparations")
    return dump(item)


@router.get("/{identifier}")
async def detail(identifier: UUID, user: User, db: DB):
    item, thread = await service_or_multiparty(db, identifier, user)
    if item is None:
        from app.platform import multiparty

        return await multiparty.detail(db, identifier, user)
    result = {**dump(item), "user_id": thread.user_id, "wakeel_id": thread.wakeel_id}
    for name, model in (("versions", AgreementVersion), ("events", AgreementEvent), ("acceptances", Acceptance)):
        result[name] = [
            dump(v)
            for v in (
                await db.scalars(select(model).where(model.agreement_id == item.id).order_by(model.created_at))
            ).all()
        ]
    people = (await db.scalars(select(Account).where(Account.id.in_([thread.user_id, thread.wakeel_id])))).all()
    result["people"] = {p.id: p.name for p in people}
    result["cases"] = [
        dump(row)
        for row in (
            await db.scalars(select(Case).join(CaseAgreement).where(CaseAgreement.agreement_id == item.id))
        ).all()
    ]
    return result


def ensure(condition, detail="This action is unavailable for the current contract status."):
    if not condition:
        raise HTTPException(409, detail)


@router.post("/{identifier}/actions")
async def act(identifier: UUID, body: AgreementAction, request: Request, user: User, db: DB):
    item, thread = await service_or_multiparty(db, identifier, user, lock=True)
    if item is None:
        from app.platform import multiparty

        return await multiparty.act(db, identifier, body, user, request)
    ensure(
        body.action not in {"approve", "reject", "open_signing", "resend_invitations"},
        "This action is only available for multi-party agreements.",
    )
    ensure(item.revision == body.revision, "The contract changed. Reload it before acting.")
    action, before = body.action, item.status
    before_version = item.current_version
    wakeel = user.id == thread.wakeel_id
    client = user.id == thread.user_id
    if wakeel and action == "sign":
        await verified_wakeel(db, user)
    if action in {
        "decline",
        "request_changes",
        "reject_changes",
        "revise",
        "amend",
        "withdraw",
        "dispute",
        "request_termination",
        "confirm_termination",
        "archive",
    }:
        ensure(len(body.reason.strip()) >= 5, "Explain the reason in at least five characters.")
    if action in {"send", "revise", "amend", "reject_changes", "withdraw"}:
        await verified_wakeel(db, user)
    if action == "send":
        ensure(wakeel and before == "DRAFT")
        item.status = "SENT"
    elif action == "review":
        ensure(client and before == "SENT")
        item.status = "UNDER_REVIEW"
    elif action == "accept":
        ensure(client and before in {"SENT", "UNDER_REVIEW", "NEGOTIATING", "AMENDMENT_PENDING"})
        item.status = "SIGNING_PENDING"
    elif action == "decline":
        ensure(client and before in {"SENT", "UNDER_REVIEW", "NEGOTIATING", "SIGNING_PENDING", "AMENDMENT_PENDING"})
        item.status = "ACTIVE" if item.active_version else "DECLINED"
        if item.active_version:
            item.current_version = item.active_version
    elif action == "request_changes":
        ensure(client and before in {"SENT", "UNDER_REVIEW", "NEGOTIATING", "SIGNING_PENDING", "AMENDMENT_PENDING"})
        item.status = "REVISION_REQUESTED"
    elif action == "reject_changes":
        ensure(wakeel and before == "REVISION_REQUESTED")
        item.status = "NEGOTIATING"
    elif action in {"revise", "amend"}:
        allowed = {
            "DRAFT",
            "SENT",
            "UNDER_REVIEW",
            "REVISION_REQUESTED",
            "NEGOTIATING",
            "SIGNING_PENDING",
            "AMENDMENT_PENDING",
        }
        ensure(before == "ACTIVE" if action == "amend" else before in allowed)
        ensure(body.terms is not None, "Provide the complete revised terms.")
        revised = parse(Terms, body.terms, "terms")
        # A rejected amendment can leave earlier drafts in history. Never reuse their version number.
        from sqlalchemy import func

        latest = await db.scalar(
            select(func.max(AgreementVersion.number)).where(AgreementVersion.agreement_id == item.id)
        )
        item.current_version = latest + 1
        add_version(db, item, user, revised)
        item.status = "AMENDMENT_PENDING" if item.active_version else "SENT"
        if before == "DRAFT":
            item.status = "DRAFT"
    elif action == "sign":
        ensure(before == "SIGNING_PENDING")
        ensure(
            body.consent and body.signed_name.casefold() == user.name.casefold(),
            "Confirm acceptance and enter your full account name.",
        )
        version = await db.scalar(
            select(AgreementVersion).where(
                AgreementVersion.agreement_id == item.id, AgreementVersion.number == item.current_version
            )
        )
        ensure(body.version_digest == version.digest, "The displayed version changed. Reload before signing.")
        previous = await db.scalar(
            select(Acceptance).where(
                Acceptance.agreement_id == item.id,
                Acceptance.version == item.current_version,
                Acceptance.user_id == user.id,
                Acceptance.kind == "signature",
            )
        )
        ensure(previous is None, "You have already signed this version.")
        db.add(
            Acceptance(
                agreement_id=item.id,
                version=item.current_version,
                user_id=user.id,
                kind="signature",
                signed_name=body.signed_name,
                digest=version.digest,
            )
        )
        await db.flush()
        signatures = (
            await db.scalars(
                select(Acceptance.user_id).where(
                    Acceptance.agreement_id == item.id,
                    Acceptance.version == item.current_version,
                    Acceptance.kind == "signature",
                )
            )
        ).all()
        if {thread.user_id, thread.wakeel_id}.issubset(signatures):
            item.active_version = item.current_version
            item.status = "ACTIVE"
            item.expires_on = version.terms["completion_date"]
    elif action == "withdraw":
        ensure(
            wakeel
            and before
            in {
                "DRAFT",
                "SENT",
                "UNDER_REVIEW",
                "NEGOTIATING",
                "REVISION_REQUESTED",
                "AMENDMENT_PENDING",
                "SIGNING_PENDING",
            }
        )
        item.status = "ACTIVE" if item.active_version else "WITHDRAWN"
        if item.active_version:
            item.current_version = item.active_version
    elif action in {"request_closure", "confirm_closure"}:
        ensure(before == ("ACTIVE" if action == "request_closure" else "CLOSURE_REQUESTED"))
        previous = await db.scalar(
            select(Acceptance).where(
                Acceptance.agreement_id == item.id,
                Acceptance.version == item.active_version,
                Acceptance.user_id == user.id,
                Acceptance.kind == "completion",
            )
        )
        ensure(previous is None, "Your completion confirmation is already recorded.")
        db.add(
            Acceptance(
                agreement_id=item.id,
                version=item.active_version,
                user_id=user.id,
                kind="completion",
                signed_name=user.name,
                digest="",
            )
        )
        item.status = "CLOSURE_REQUESTED"
        await db.flush()
        parties = (
            await db.scalars(
                select(Acceptance.user_id).where(
                    Acceptance.agreement_id == item.id,
                    Acceptance.version == item.active_version,
                    Acceptance.kind == "completion",
                )
            )
        ).all()
        if {thread.user_id, thread.wakeel_id}.issubset(parties):
            item.status = "COMPLETED"
    elif action in {"request_termination", "confirm_termination"}:
        ensure(before == ("ACTIVE" if action == "request_termination" else "TERMINATION_PENDING"))
        previous = await db.scalar(
            select(Acceptance).where(
                Acceptance.agreement_id == item.id,
                Acceptance.version == item.active_version,
                Acceptance.user_id == user.id,
                Acceptance.kind == "termination",
            )
        )
        ensure(previous is None, "Your termination confirmation is already recorded.")
        db.add(
            Acceptance(
                agreement_id=item.id,
                version=item.active_version,
                user_id=user.id,
                kind="termination",
                signed_name=user.name,
                digest="",
            )
        )
        item.status = "TERMINATION_PENDING"
        await db.flush()
        parties = (
            await db.scalars(
                select(Acceptance.user_id).where(
                    Acceptance.agreement_id == item.id,
                    Acceptance.version == item.active_version,
                    Acceptance.kind == "termination",
                )
            )
        ).all()
        if {thread.user_id, thread.wakeel_id}.issubset(parties):
            item.status = "TERMINATED"
    elif action == "archive":
        ensure(before in {"COMPLETED", "CANCELLED", "TERMINATED", "EXPIRED", "WITHDRAWN", "DECLINED"})
        item.status = "ARCHIVED"
    elif action == "dispute":
        ensure(
            before in {"ACTIVE", "CLOSURE_REQUESTED", "AMENDMENT_PENDING", "SIGNING_PENDING"}
            and item.active_version is not None
        )
        item.status = "DISPUTED"
    item.revision += 1
    event_version = before_version if action in {"decline", "withdraw"} else item.current_version
    event(db, item, user, action, before, body.reason, version=event_version)
    if before != "DRAFT" or action == "send":
        await notify(
            db, other_party(thread, user), f"{item.title}: {action.replace('_', ' ')}", f"/agreements/{item.id}"
        )
    from app.platform.events import invalidate
    from app.platform.mail import enqueue

    await enqueue(db, user.id, "agreements", f"{item.title}: {action.replace('_', ' ')}", f"/agreements/{item.id}")
    case_ids = (await db.scalars(select(CaseAgreement.case_id).where(CaseAgreement.agreement_id == item.id))).all()
    invalidate(
        db,
        [thread.user_id, thread.wakeel_id],
        "/agreements",
        f"/agreements/{item.id}",
        "/cases",
        *[f"/cases/{cid}" for cid in case_ids],
    )

    return dump(item)
