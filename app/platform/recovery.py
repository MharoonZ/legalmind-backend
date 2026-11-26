"""Five-minute, single-use OTP password recovery."""

import hmac
import secrets
import time
from datetime import UTC, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.api.deps import get_database
from app.platform.mail import configured, enqueue, otp_digest, otp_value
from app.platform.models import Account, OtpChallenge, ResetToken, now
from app.platform.schemas import EmailInput, OtpVerify
from app.platform.security import DB, digest, throttle

router = APIRouter(prefix="/auth", tags=["account recovery"])


@router.post("/forgot-password")
async def request_otp(body: EmailInput, request: Request, db: DB):
    settings = get_settings()
    if not configured(settings) or len(settings.otp_secret) < 32:
        raise HTTPException(503, "Email recovery is not configured. Please contact support.")
    await throttle(request, "otp_request_ip", request.client.host, 10)
    await throttle(request, "otp_request_account", body.email, 3)
    # Enforce the rolling cooldown for every address, including nonexistent ones.
    # This keeps resends usable and avoids disclosing account existence.
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
                    "cutoff": int(time.time()) - 60,
                },
            )
        ).scalar()
    if reserved is None:
        raise HTTPException(
            429, "Please wait 60 seconds before requesting another code.", headers={"Retry-After": "60"}
        )
    identifier = str(uuid4())
    reply = {
        "message": "If this email has an active account, a code will arrive shortly.",
        "challenge_id": identifier,
        "expires_in": 300,
        "resend_after": 60,
    }
    user = await db.scalar(
        select(Account).where(Account.email == body.email, Account.active.is_(True)).with_for_update()
    )
    if user:
        await db.execute(delete(OtpChallenge).where(OtpChallenge.user_id == user.id))
        code = otp_value(identifier, user.id, settings)
        db.add(
            OtpChallenge(
                id=identifier,
                user_id=user.id,
                otp_hash=otp_digest(identifier, code, settings),
                expires_at=now() + timedelta(minutes=5),
            )
        )
        await db.flush()
        await enqueue(
            db,
            user.id,
            "otp",
            "Your LegalMind password reset code",
            "/forgot-password",
            payload={"challenge_id": identifier},
        )
    return reply


@router.post("/verify-otp")
async def verify_otp(body: OtpVerify, request: Request, db: DB):
    await throttle(request, "otp_verify_ip", request.client.host, 20)
    await throttle(request, "otp_verify_account", body.email, 10)
    # Lock the account first, consistently with resend/reset, to prevent races.
    user = await db.scalar(
        select(Account).where(Account.email == body.email, Account.active.is_(True)).with_for_update()
    )
    row = await db.scalar(select(OtpChallenge).where(OtpChallenge.id == str(body.challenge_id)).with_for_update())
    invalid = JSONResponse(
        {"detail": "The code is incorrect, expired, or already used. Request a new code."}, status_code=400
    )
    if (
        not row
        or not user
        or row.user_id != user.id
        or row.used
        or row.attempts >= 5
        or row.expires_at.replace(tzinfo=UTC) <= now()
    ):
        return invalid
    row.attempts += 1
    if not hmac.compare_digest(row.otp_hash, otp_digest(row.id, body.otp)):
        return invalid  # Return, rather than raise, so the failed attempt commits.
    row.used = True
    await db.execute(delete(ResetToken).where(ResetToken.user_id == user.id))
    token = secrets.token_urlsafe(32)
    db.add(ResetToken(user_id=user.id, token_hash=digest(token), expires_at=now() + timedelta(minutes=5)))
    return {"token": token, "expires_in": 300}
