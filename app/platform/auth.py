import asyncio
from datetime import UTC, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.platform.models import Account, OtpChallenge, ResetToken, WakeelProfile, now
from app.platform.schemas import EmailPreferences, Login, PasswordChange, Profile, Register, ResetPassword
from app.platform.security import (
    COOKIE,
    DB,
    User,
    audit,
    cookie,
    digest,
    hash_password,
    new_session,
    password_matches,
    public_account,
    throttle,
    revoke_sessions,
)

from app.platform.mail import enqueue

router = APIRouter(prefix="/auth", tags=["accounts"])
# Use a real hash to keep the nonexistent-account path equally expensive.
_DUMMY_HASH = hash_password("unused-login-timing-placeholder")


@router.post("/register", status_code=201)
async def register(body: Register, request: Request, response: Response, db: DB):
    await throttle(request, "register", request.client.host, 5)
    user = Account(
        email=body.email,
        name=body.name,
        role=body.role,
        city=body.city,
        password_hash=await asyncio.to_thread(hash_password, body.password),
    )
    try:
        async with db.begin_nested():
            db.add(user)
            await db.flush()
            if user.role == "wakeel":
                db.add(
                    WakeelProfile(
                        user_id=user.id,
                        bar_number=body.bar_number,
                        specialization=body.specialization,
                        experience=body.experience,
                    )
                )
            await db.flush()
    except IntegrityError:
        raise HTTPException(409, "An account with these registration details already exists.") from None
    await new_session(db, user, response)
    audit(db, user, "account_registered", user.id)
    await enqueue(db, user.id, "account", "Welcome to LegalMind", payload={"welcome": True})
    return public_account(user)


@router.post("/login")
async def login(body: Login, request: Request, response: Response, db: DB):
    await throttle(request, "login_ip", request.client.host, 20)
    await throttle(request, "login_account", body.email, 10)
    user = await db.scalar(select(Account).where(Account.email == body.email))
    correct = await password_matches(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not correct or not user.active:
        raise HTTPException(401, "Email or password is incorrect, or the account is unavailable.")
    old = request.cookies.get(COOKIE)
    if old:
        await revoke_sessions(db, token=old)
    await new_session(db, user, response)
    audit(db, user, "login", user.id)
    return public_account(user)


@router.get("/me")
async def me(user: User, db: DB):
    data = public_account(user)
    profile = await db.get(WakeelProfile, user.id) if user.role == "wakeel" else None
    if profile:
        data["professional"] = {
            key: getattr(profile, key)
            for key in (
                "bar_number",
                "specialization",
                "experience",
                "biography",
                "verification",
                "verification_reason",
            )
        }
    return data


@router.post("/refresh")
async def refresh(request: Request, response: Response, user: User, db: DB):
    session = request.state.platform_session
    session.expires_at = min(now() + timedelta(days=7), session.absolute_expires_at.replace(tzinfo=UTC))
    cookie(response, request.cookies[COOKIE])
    return public_account(user)


@router.post("/logout")
async def logout(request: Request, response: Response, db: DB):
    token = request.cookies.get(COOKIE)
    if token:
        await revoke_sessions(db, token=token)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/profile")
async def profile(body: Profile, user: User, db: DB):
    user.name, user.city = body.name, body.city
    professional = await db.get(WakeelProfile, user.id)
    if professional:
        professional.biography = body.biography
        professional.specialization = body.specialization
        professional.experience = body.experience
    from app.platform.events import directory_changed, invalidate

    if professional:
        directory_changed(db)
    invalidate(db, [user.id], "/auth/me")
    audit(db, user, "profile_updated", user.id)
    return public_account(user)


@router.post("/password")
async def password(body: PasswordChange, request: Request, response: Response, user: User, db: DB):
    await throttle(request, "change_password", user.id, 5)
    if not await password_matches(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect.")
    user.password_hash = await asyncio.to_thread(hash_password, body.password)
    await revoke_sessions(db, user_id=user.id)
    await new_session(db, user, response)
    await db.execute(delete(ResetToken).where(ResetToken.user_id == user.id))
    await db.execute(delete(OtpChallenge).where(OtpChallenge.user_id == user.id))
    await enqueue(db, user.id, "security", "Your LegalMind password was changed")
    audit(db, user, "password_changed", user.id)
    return {"ok": True}


@router.post("/reset")
async def reset(body: ResetPassword, request: Request, db: DB):
    await throttle(request, "reset", request.client.host, 10)
    token = await db.scalar(select(ResetToken).where(ResetToken.token_hash == digest(body.token)))
    if not token or token.expires_at.replace(tzinfo=UTC) <= now():
        raise HTTPException(400, "This password reset link is invalid or expired.")
    user = await db.scalar(select(Account).where(Account.id == token.user_id).with_for_update())
    if not user or not user.active:
        raise HTTPException(400, "This password reset link is invalid or expired.")
    # Recheck after the account lock: another reset may have consumed the token.
    token = await db.scalar(
        select(ResetToken)
        .where(ResetToken.token_hash == digest(body.token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not token or token.expires_at.replace(tzinfo=UTC) <= now():
        raise HTTPException(400, "This password reset link is invalid or expired.")
    user.password_hash = await asyncio.to_thread(hash_password, body.password)
    await revoke_sessions(db, user_id=user.id)
    await db.execute(delete(ResetToken).where(ResetToken.user_id == user.id))
    await db.execute(delete(OtpChallenge).where(OtpChallenge.user_id == user.id))
    await enqueue(db, user.id, "security", "Your LegalMind password was reset")
    audit(db, user, "password_reset", user.id)
    return {"ok": True}


@router.get("/email-preferences")
async def preferences(user: User):
    return EmailPreferences(**(user.email_preferences or {}))


@router.post("/email-preferences")
async def save_preferences(body: EmailPreferences, user: User, db: DB):
    from app.platform.events import invalidate

    user.email_preferences = body.model_dump()
    invalidate(db, [user.id], "/auth/email-preferences")
    return body
