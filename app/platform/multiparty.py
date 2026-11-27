"""Multi-party agreements: invited parties, per-version approvals and signatures.

The creator (a party, or a Wakeel drafting for others) writes the terms. A
drafting Wakeel is not a signatory unless listed as a party. Agreement contents
are visible only to the creator and to parties who accepted their invitation.
Versions are immutable; a revision always creates a new version and resets
approvals, so a signature is permanently tied to the exact text that was signed.
"""

import secrets
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, or_, select

from app.platform.agreements import add_version, ensure, event
from app.platform.common import dump, relationship
from app.platform.events import invalidate
from app.platform.mail import enqueue
from app.platform.models import (
    Acceptance,
    Account,
    Agreement,
    AgreementEvent,
    AgreementParty,
    AgreementVersion,
    Notification,
    now,
)
from app.platform.schemas import AgreementAction, InvitationResponse, MultiPartyIn, PartyTerms, parse
from app.platform.security import DB, User, audit, digest, notify, password_matches, throttle

router = APIRouter(tags=["multi-party agreements"])

REVIEWABLE = {"SENT", "UNDER_REVIEW", "REVISION_REQUESTED", "APPROVED"}
EDITABLE = {"DRAFT", "SENT", "UNDER_REVIEW", "REVISION_REQUESTED", "APPROVED", "SIGNING_PENDING", "PARTIALLY_SIGNED"}
SIGNING = {"SIGNING_PENDING", "PARTIALLY_SIGNED"}


def today():
    return now().astimezone(ZoneInfo("Asia/Karachi")).date()


async def parties_of(db, item):
    return (
        await db.scalars(
            select(AgreementParty).where(AgreementParty.agreement_id == item.id).order_by(AgreementParty.created_at)
        )
    ).all()


async def load(db, identifier, user, lock=False):
    """Return (agreement, parties, the caller's own party row or None). 404 for everyone else."""
    query = select(Agreement).where(Agreement.id == str(identifier), Agreement.kind == "multi_party")
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query)
    if not item:
        raise HTTPException(404, "This agreement could not be found.")
    parties = await parties_of(db, item)
    mine = next((p for p in parties if p.account_id == user.id and p.invite_status == "ACCEPTED"), None)
    if not mine and item.creator_id != user.id:
        raise HTTPException(404, "This agreement could not be found.")
    if item.status == "DRAFT" and item.creator_id != user.id:
        raise HTTPException(404, "This agreement could not be found.")
    return item, parties, mine


def participants(item, parties):
    """Accounts allowed to see the agreement."""
    ids = {p.account_id for p in parties if p.account_id and p.invite_status == "ACCEPTED"}
    return list(ids | {item.creator_id})


def party_dump(party):
    return dump(party, ("invite_token_hash",))


def allowed_actions(item, parties, mine, user):
    creator = item.creator_id == user.id
    signer = bool(mine and mine.signatory)
    status = item.status
    actions = set()
    if creator:
        if status == "DRAFT":
            actions |= {"send"}
        if status in EDITABLE:
            actions |= {"revise", "withdraw"}
        if status in REVIEWABLE | SIGNING:
            actions.add("resend_invitations")
        if status == "APPROVED":
            actions.add("open_signing")
    if signer:
        if status == "SENT":
            actions.add("review")
        if status in REVIEWABLE and not (mine.approval == "APPROVED" and mine.approval_version == item.current_version):
            actions |= {"approve", "request_changes", "reject"}
        if status in SIGNING and mine.signed_version != item.current_version:
            actions |= {"sign", "request_changes"}
        if status in {"ACTIVE", "CLOSURE_REQUESTED"}:
            actions.add("request_closure" if status == "ACTIVE" else "confirm_closure")
        if status in {"ACTIVE", "TERMINATION_PENDING"}:
            actions.add("request_termination" if status == "ACTIVE" else "confirm_termination")
    return sorted(actions)


async def invite(db, item, party, inviter):
    """Email one party. People without an account must follow the emailed link to prove the address is theirs."""
    account = await db.scalar(select(Account).where(Account.email == party.email, Account.active.is_(True)))
    party.invited_at = now()
    payload = {"inviter": inviter.name, "role": party.role, "party_id": party.id, "name": party.name}
    title = f"{inviter.name} invited you to an agreement on LegalMind"
    if account:
        party.account_id = account.id
        party.invite_token_hash = digest(secrets.token_urlsafe(32))  # marks the invitation as open
        db.add(Notification(user_id=account.id, title=title[:180], href="/agreements/invitations"))
        invalidate(db, [account.id], "/notifications", "/agreement-invitations", "/dashboard")
        await enqueue(db, account.id, "invite", title, "/dashboard", payload=payload)
    else:
        token = secrets.token_urlsafe(32)
        party.invite_token_hash = digest(token)
        # The plaintext token lives only in the outbox until the email is sent.
        await enqueue(db, None, "invite", title, "/dashboard", payload={**payload, "token": token}, email=party.email)


@router.post("/agreements/multi-party", status_code=201)
async def create(body: MultiPartyIn, request: Request, user: User, db: DB):
    if user.role == "admin":
        raise HTTPException(403, "Administrators cannot create agreements.")
    await throttle(request, "agreement_create", user.id, 10)
    workspace = await relationship(db, body.relationship_id, user) if body.relationship_id else None
    reference = "LM-" + now().strftime("%Y") + "-" + secrets.token_hex(3).upper()
    item = Agreement(
        kind="multi_party",
        creator_id=user.id,
        relationship_id=workspace.id if workspace else None,
        title=body.terms.title,
        category=body.terms.agreement_type,
        reference=reference,
    )
    db.add(item)
    await db.flush()
    add_version(db, item, user, body.terms)
    for entry in body.parties:
        own = entry.email == user.email
        db.add(
            AgreementParty(
                agreement_id=item.id,
                account_id=user.id if own else None,
                invite_status="ACCEPTED" if own else "PENDING",
                responded_at=now() if own else None,
                **entry.model_dump(),
            )
        )
    event(db, item, user, "create", "DRAFT")
    await db.flush()
    invalidate(db, [user.id], "/agreements", "/dashboard")
    return dump(item)


async def detail(db, identifier, user):
    item, parties, mine = await load(db, identifier, user)
    result = {**dump(item), "parties": [party_dump(p) for p in parties]}
    for name, model in (("versions", AgreementVersion), ("events", AgreementEvent), ("acceptances", Acceptance)):
        result[name] = [
            dump(row)
            for row in (
                await db.scalars(select(model).where(model.agreement_id == item.id).order_by(model.created_at))
            ).all()
        ]
    ids = (
        {p.account_id for p in parties if p.account_id} | {item.creator_id} | {e["actor_id"] for e in result["events"]}
    )
    people = (await db.execute(select(Account.id, Account.name).where(Account.id.in_(ids)))).all()
    result["people"] = dict(people)
    result["my_party_id"] = mine.id if mine else None
    result["actions"] = allowed_actions(item, parties, mine, user)
    result["drafting_only"] = item.creator_id == user.id and mine is None
    return result


def reset_approvals(parties):
    for party in parties:
        party.approval, party.approval_note = "PENDING", ""


def signatories(parties):
    return [p for p in parties if p.signatory]


async def confirmations(db, item, kind):
    return set(
        (
            await db.scalars(
                select(Acceptance.user_id).where(
                    Acceptance.agreement_id == item.id,
                    Acceptance.version == item.active_version,
                    Acceptance.kind == kind,
                )
            )
        ).all()
    )


async def act(db, identifier, body: AgreementAction, user, request):
    item, parties, mine = await load(db, identifier, user, lock=True)
    ensure(item.revision == body.revision, "This agreement changed. Reload it before acting.")
    action, before, before_version = body.action, item.status, item.current_version
    ensure(
        action in allowed_actions(item, parties, mine, user),
        "This action is not available for the current agreement status.",
    )
    reason = body.reason.strip()
    if action in {"request_changes", "reject", "revise", "withdraw", "request_termination"}:
        ensure(len(reason) >= 5, "Explain the reason in at least five characters.")
    signers = signatories(parties)
    if action == "send":
        for party in parties:
            if party.invite_status == "PENDING":
                await invite(db, item, party, user)
        item.status = "SENT"
    elif action == "resend_invitations":
        pending = [p for p in parties if p.invite_status == "PENDING"]
        ensure(bool(pending), "Every party has already responded to their invitation.")
        for party in pending:
            await invite(db, item, party, user)
    elif action == "review":
        item.status = "UNDER_REVIEW"
    elif action == "approve":
        mine.approval, mine.approval_version, mine.approval_note = "APPROVED", item.current_version, reason
        if all(
            p.invite_status == "ACCEPTED" and p.approval == "APPROVED" and p.approval_version == item.current_version
            for p in signers
        ):
            item.status = "APPROVED"
        elif before == "SENT":
            item.status = "UNDER_REVIEW"
    elif action == "request_changes":
        mine.approval, mine.approval_version, mine.approval_note = "CHANGES_REQUESTED", item.current_version, reason
        item.status = "REVISION_REQUESTED"
    elif action == "reject":
        mine.approval, mine.approval_version, mine.approval_note = "REJECTED", item.current_version, reason
        item.status = "DECLINED"
    elif action == "revise":
        ensure(body.terms is not None, "Provide the complete revised terms.")
        terms = parse(PartyTerms, body.terms, "terms")
        latest = await db.scalar(
            select(func.max(AgreementVersion.number)).where(AgreementVersion.agreement_id == item.id)
        )
        item.current_version = latest + 1
        add_version(db, item, user, terms)
        item.category = terms.agreement_type
        # Earlier signatures stay on their own version; the new text needs fresh approvals.
        reset_approvals(parties)
        item.status = "DRAFT" if before == "DRAFT" else "SENT"
    elif action == "open_signing":
        ensure(
            all(p.invite_status == "ACCEPTED" for p in signers),
            "Every signing party must accept their invitation first.",
        )
        item.status = "SIGNING_PENDING"
    elif action == "sign":
        # Identity: an authenticated session plus the account password at the moment of signing.
        await throttle(request, "agreement_sign", user.id, 10)
        ensure(bool(body.password), "Enter your account password to sign.")
        if not await password_matches(body.password, user.password_hash):
            raise HTTPException(403, "Your password is incorrect. The agreement was not signed.")
        ensure(
            body.consent and body.signed_name.casefold() == user.name.casefold(),
            "Confirm your consent and type your full account name exactly.",
        )
        version = await db.scalar(
            select(AgreementVersion).where(
                AgreementVersion.agreement_id == item.id, AgreementVersion.number == item.current_version
            )
        )
        ensure(body.version_digest == version.digest, "The displayed version changed. Reload before signing.")
        db.add(
            Acceptance(
                agreement_id=item.id,
                version=item.current_version,
                user_id=user.id,
                kind="signature",
                signed_name=body.signed_name,
                digest=version.digest,
                evidence={
                    "method": "password re-authentication",
                    "ip": request.client.host if request.client else "",
                    "user_agent": request.headers.get("user-agent", "")[:300],
                    "party_id": mine.id,
                    "party_role": mine.role,
                    "consent": "I have read this version and agree to be bound by it.",
                },
            )
        )
        mine.signed_version = item.current_version
        if all(p.signed_version == item.current_version for p in signers):
            item.active_version = item.current_version
            item.expires_on = version.terms.get("expiry_date")
            effective = date.fromisoformat(version.terms["effective_date"])
            item.status = "ACTIVE" if effective <= today() else "FULLY_SIGNED"
        else:
            item.status = "PARTIALLY_SIGNED"
    elif action == "withdraw":
        item.status = "WITHDRAWN"
    elif action in {"request_closure", "confirm_closure", "request_termination", "confirm_termination"}:
        kind = "completion" if "closure" in action else "termination"
        done = await confirmations(db, item, kind)
        ensure(user.id not in done, "Your confirmation is already recorded.")
        db.add(
            Acceptance(
                agreement_id=item.id,
                version=item.active_version,
                user_id=user.id,
                kind=kind,
                signed_name=user.name,
                digest="",
            )
        )
        done.add(user.id)
        everyone = {p.account_id for p in signers} <= done
        if kind == "completion":
            item.status = "COMPLETED" if everyone else "CLOSURE_REQUESTED"
        else:
            item.status = "TERMINATED" if everyone else "TERMINATION_PENDING"
    item.revision += 1
    event(
        db, item, user, action, before, reason, version=before_version if action == "withdraw" else item.current_version
    )
    await db.flush()
    recipients = participants(item, parties)
    if item.status != "DRAFT":
        label = {
            "send": "was sent for review",
            "revise": "was revised",
            "approve": "was approved by " + user.name,
            "request_changes": "has requested changes",
            "reject": "was rejected by " + user.name,
            "open_signing": "is ready to sign",
            "sign": "was signed by " + user.name,
            "withdraw": "was withdrawn",
        }.get(action, action.replace("_", " "))
        for recipient in recipients:
            if recipient != user.id:
                await notify(db, recipient, f"{item.title} {label}"[:180], f"/agreements/{item.id}")
    invalidate(db, recipients, "/agreements", f"/agreements/{item.id}", "/dashboard")
    if item.relationship_id:
        invalidate(db, recipients, f"/relationships/{item.relationship_id}")
    return dump(item)


# ---------------------------------------------------------------- invitations
@router.get("/agreement-invitations")
async def invitations(user: User, db: DB):
    """Open invitations addressed to the signed-in account. No agreement contents are included."""
    rows = (
        await db.execute(
            select(AgreementParty, Account.name)
            .join(Agreement, Agreement.id == AgreementParty.agreement_id)
            .join(Account, Account.id == Agreement.creator_id)
            .where(
                AgreementParty.account_id == user.id,
                AgreementParty.invite_status == "PENDING",
                AgreementParty.invite_token_hash.is_not(None),
                Agreement.status.not_in(["DRAFT", "WITHDRAWN", "DECLINED"]),
            )
            .order_by(AgreementParty.invited_at.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "id": party.id,
            "role": party.role,
            "signatory": party.signatory,
            "invited_at": party.invited_at,
            "inviter": name,
        }
        for party, name in rows
    ]


async def respond(db, party, item, user, action, reason):
    ensure(party.invite_status == "PENDING", "You have already responded to this invitation.")
    ensure(item.status not in {"DRAFT", "WITHDRAWN", "DECLINED"}, "This invitation is no longer open.")
    party.account_id = user.id
    party.invite_status = "ACCEPTED" if action == "accept" else "DECLINED"
    party.responded_at, party.invite_token_hash = now(), None
    if action == "decline":
        party.approval_note = reason
    item.revision += 1
    event(db, item, user, "invitation_" + action, item.status, reason)
    audit(db, user, "agreement_invitation_" + action, item.id)
    parties = await parties_of(db, item)
    await db.flush()
    for recipient in participants(item, parties):
        if recipient != user.id:
            await notify(
                db,
                recipient,
                f"{user.name} {'accepted' if action == 'accept' else 'declined'} an agreement invitation",
                f"/agreements/{item.id}",
            )
    invalidate(
        db,
        [*participants(item, parties), user.id],
        "/agreements",
        f"/agreements/{item.id}",
        "/agreement-invitations",
        "/dashboard",
    )
    return {"agreement_id": item.id if action == "accept" else None, "status": party.invite_status}


@router.post("/agreement-invitations/respond")
async def respond_with_token(body: InvitationResponse, request: Request, user: User, db: DB):
    """Accept or decline from the emailed link. Possession of the link proves control of the mailbox."""
    await throttle(request, "invitation", user.id, 20)
    party = await db.scalar(
        select(AgreementParty).where(AgreementParty.invite_token_hash == digest(body.token)).with_for_update()
    )
    if not party:
        raise HTTPException(404, "This invitation link is invalid or has already been used.")
    if party.email != user.email:
        raise HTTPException(
            403, "This invitation was sent to a different email address. Sign in with the invited email to respond."
        )
    item = await db.scalar(select(Agreement).where(Agreement.id == party.agreement_id).with_for_update())
    return await respond(db, party, item, user, body.action, body.reason)


@router.post("/agreement-invitations/{identifier}/{action}")
async def respond_in_app(identifier: UUID, action: str, user: User, db: DB):
    """Registered accounts that were matched by email when the invitation was sent."""
    if action not in {"accept", "decline"}:
        raise HTTPException(404, "Not found.")
    party = await db.scalar(
        select(AgreementParty)
        .where(AgreementParty.id == str(identifier), AgreementParty.account_id == user.id)
        .with_for_update()
    )
    if not party or party.email != user.email:
        raise HTTPException(404, "This invitation could not be found.")
    item = await db.scalar(select(Agreement).where(Agreement.id == party.agreement_id).with_for_update())
    return await respond(db, party, item, user, action, "")


def visible_to(user):
    """SQL condition: multi-party agreements the account may list."""
    accepted = select(AgreementParty.agreement_id).where(
        AgreementParty.account_id == user.id, AgreementParty.invite_status == "ACCEPTED"
    )
    return (Agreement.kind == "multi_party") & or_(
        Agreement.creator_id == user.id, Agreement.id.in_(accepted) & (Agreement.status != "DRAFT")
    )
