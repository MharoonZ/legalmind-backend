"""Email OTP password recovery.

Request code -> verify code -> one short-lived reset token -> new password.

Security properties:
- The code is derived with HMAC from a server secret and never stored or logged;
  only an HMAC digest of it is kept for comparison.
- Unknown addresses get a decoy challenge, so every response (including expiry
  and lock-out) is identical for registered and unregistered emails.
- Codes are single use, expire after OTP_EXPIRY_MINUTES and lock after
  OTP_MAX_ATTEMPTS wrong guesses. Requests are throttled per IP and per address.
- A verified code yields a separate hashed, single-use reset token; an
  unverified code can never change a password.
"""

import hmac
import secrets
import time
from datetime import UTC, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text

from app.api.deps import get_database
from app.core.config import get_settings
from app.platform.mail import configured, enqueue, otp_digest, otp_value
from app.platform.models import Account, OtpChallenge, ResetToken, now
from app.platform.schemas import EmailInput, OtpResend, OtpVerify
from app.platform.security import DB, audit, digest, throttle

router = APIRouter(prefix="/auth", tags=["account recovery"])

GENERIC = "If an account exists with this email address, a verification code will be sent."


def failure(code, detail, status=400, **extra):
    # Returned, not raised, so the failed attempt is committed with the response.
    return JSONResponse({"detail": detail, "code": code, **extra}, status_code=status)


async def issue(body, request: Request, db):
    settings = get_settings()
    if not configured(settings) or len(settings.otp_secret) < 32:
        raise HTTPException(503, "Password recovery by email is not available right now. Please contact support.")
    await throttle(request, "otp_request_ip", request.client.host, 10)
    await throttle(request, "otp_request_account", body.email, 3)
    cooldown = settings.otp_resend_seconds
    # The rolling cooldown applies to every address, registered or not.
    database = get_database(request)
    async with database.engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        reserved = (
            await connection.execute(
                text(
                    "INSERT INTO platform_rate_buckets (key, window_start, attempts) VALUES (:key, :stamp, 1) "
                    "ON CONFLICT (key) DO UPDATE SET window_start=:stamp, attempts=1 "
                    "WHERE platform_rate_buckets.window_start <= :cutoff RETURNING attempts"
                ),
                {
                    "key": digest("otp_cooldown:" + body.email),
                    "stamp": int(time.time()),
                    "cutoff": int(time.time()) - cooldown,
                },
            )
        ).scalar()
    if reserved is None:
        raise HTTPException(
            429,
            f"Please wait {cooldown} seconds before requesting another code.",
            headers={"Retry-After": str(cooldown)},
        )
    identifier = str(uuid4())
    expires = settings.otp_expiry_minutes * 60
    # Opportunistic cleanup keeps decoys and stale challenges from accumulating.
    await db.execute(delete(OtpChallenge).where(OtpChallenge.expires_at < now() - timedelta(hours=1)))
    user = await db.scalar(
        select(Account).where(Account.email == body.email, Account.active.is_(True)).with_for_update()
    )
    if user:
        # A new code always invalidates the previous one.
        await db.execute(delete(OtpChallenge).where(OtpChallenge.user_id == user.id))
        await db.execute(delete(ResetToken).where(ResetToken.user_id == user.id))
        code = otp_value(identifier, user.id, settings)
        db.add(
            OtpChallenge(
                id=identifier,
                user_id=user.id,
                otp_hash=otp_digest(identifier, code, settings),
                expires_at=now() + timedelta(seconds=expires),
            )
        )
        await db.flush()
        await enqueue(
            db,
            user.id,
            "otp",
            "Your LegalMind verification code",
            "/forgot-password",
            payload={"challenge_id": identifier},
        )
        audit(db, user, "password_reset_requested", user.id)
    else:
        db.add(
            OtpChallenge(
                id=identifier,
                user_id=None,
                otp_hash=secrets.token_hex(32),
                expires_at=now() + timedelta(seconds=expires),
            )
        )
    return {
        "message": GENERIC,
        "challenge_id": identifier,
        "expires_in": expires,
        "resend_after": cooldown,
        "max_attempts": settings.otp_max_attempts,
    }


@router.post("/forgot-password")
async def request_otp(body: EmailInput, request: Request, db: DB):
    return await issue(body, request, db)


@router.post("/resend-otp")
async def resend_otp(body: OtpResend, request: Request, db: DB):
    """Issue a fresh code for the same address. The previous code stops working."""
    return await issue(body, request, db)


@router.post("/verify-otp")
async def verify_otp(body: OtpVerify, request: Request, db: DB):
    settings = get_settings()
    await throttle(request, "otp_verify_ip", request.client.host, 20)
    await throttle(request, "otp_verify_account", body.email, 10)
    # Lock the account first, consistently with resend/reset, to prevent races.
    user = await db.scalar(
        select(Account).where(Account.email == body.email, Account.active.is_(True)).with_for_update()
    )
    row = await db.scalar(select(OtpChallenge).where(OtpChallenge.id == str(body.challenge_id)).with_for_update())
    # The challenge must belong to this reset request: same address, same challenge id.
    if not row or row.used or row.user_id != (user.id if user else None):
        return failure("otp_invalid", "This verification code is no longer valid. Please request a new code.")
    if row.expires_at.replace(tzinfo=UTC) <= now():
        return failure("otp_expired", "Your verification code has expired. Please request a new code.")
    if row.attempts >= settings.otp_max_attempts:
        return failure("otp_locked", "Too many incorrect attempts. Please request a new code.", 429)
    row.attempts += 1
    remaining = settings.otp_max_attempts - row.attempts
    if not user or not hmac.compare_digest(row.otp_hash, otp_digest(row.id, body.otp, settings)):
        if remaining <= 0:
            return failure("otp_locked", "Too many incorrect attempts. Please request a new code.", 429)
        return failure(
            "otp_incorrect", "The verification code is incorrect. Please try again.", attempts_remaining=remaining
        )
    row.used = True
    await db.execute(delete(ResetToken).where(ResetToken.user_id == user.id))
    token = secrets.token_urlsafe(32)
    db.add(ResetToken(user_id=user.id, token_hash=digest(token), expires_at=now() + timedelta(minutes=10)))
    audit(db, user, "password_reset_verified", user.id)
    return {"token": token, "expires_in": 600}
