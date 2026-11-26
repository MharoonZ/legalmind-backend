"""Opaque, revocable server sessions. No browser-readable authentication tokens."""

import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import UTC, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_database
from app.core.config import get_settings
from app.platform.models import Account, Audit, Notification, Session, now

COOKIE = "legalmind_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    return salt + ":" + value.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":")
        value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError):
        return False


def csrf(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    settings = get_settings()
    origin = request.headers.get("origin")
    if request.headers.get("x-legalmind-client") != "web" or (origin and origin not in settings.cors_origins_list):
        raise HTTPException(403, "Untrusted request origin.")


async def transaction(db=Depends(get_database)):
    async with db.session() as session:
        async with session.begin():
            yield session


DB = Annotated[AsyncSession, Depends(transaction, scope="function")]


async def current_user(request: Request, db: DB) -> Account:
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 200:
        raise HTTPException(401, "Please sign in.")
    record = await db.scalar(select(Session).where(Session.token_hash == digest(token)))
    if (
        record is None
        or record.expires_at.replace(tzinfo=UTC) <= now()
        or record.absolute_expires_at.replace(tzinfo=UTC) <= now()
    ):
        raise HTTPException(401, "Your session expired. Please sign in.")
    account = await db.get(Account, record.user_id)
    if not account or not account.active:
        raise HTTPException(401, "Your account is unavailable.")
    request.state.platform_session = record
    return account


User = Annotated[Account, Depends(current_user)]


def require_role(user: Account, *roles: str):
    if user.role not in roles:
        raise HTTPException(403, "Your account cannot perform this action.")


def cookie(response, token: str):
    response.set_cookie(
        COOKIE, token, httponly=True, secure=get_settings().is_production, samesite="lax", max_age=7 * 86400, path="/"
    )


async def new_session(db, user, response):
    token = secrets.token_urlsafe(48)
    db.add(
        Session(
            user_id=user.id,
            token_hash=digest(token),
            expires_at=now() + timedelta(days=7),
            absolute_expires_at=now() + timedelta(days=30),
        )
    )
    cookie(response, token)


async def throttle(request, scope: str, identity: str, maximum: int):
    # Separate committed counters survive failed logins and work across API workers.
    db = get_database(request)
    key = digest(scope + ":" + identity)
    window = int(time.time() // 60)
    async with db.session() as session:
        async with session.begin():
            count = (
                await session.execute(
                    text(
                        "INSERT INTO platform_rate_buckets (key, window_start, attempts) VALUES (:key, :window, 1) "
                        "ON CONFLICT (key) DO UPDATE SET window_start = :window, attempts = "
                        "CASE WHEN platform_rate_buckets.window_start = :window THEN platform_rate_buckets.attempts + 1 ELSE 1 END "
                        "RETURNING attempts"
                    ),
                    {"key": key, "window": window},
                )
            ).scalar()
    if count > maximum:
        raise HTTPException(429, "Too many attempts. Please wait a minute.", headers={"Retry-After": "60"})


def audit(db, user, action, resource=None, detail=""):
    db.add(Audit(actor_id=user.id if user else None, action=action, resource_id=resource, detail=detail))


def notify(db, recipient, title, href):
    db.add(Notification(user_id=recipient, title=title[:180], href=href))


def public_account(user):
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "city": user.city}


async def password_matches(password, stored):
    return await asyncio.to_thread(verify_password, password, stored)
