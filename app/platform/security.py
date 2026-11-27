"""Opaque, revocable server sessions. No browser-readable authentication tokens."""

import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select, text
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


async def transaction(request: Request, db=Depends(get_database)):
    async with db.session() as session:
        async with session.begin():
            yield session
            events = session.info.get("platform_events", [])
            hub = getattr(request.app.state, "realtime", None)
            if hub and events:
                await hub.publish(session, events)
        if hub and events:
            await hub.after_commit(events)
        # Deliver queued email promptly instead of waiting for the next poll.
        worker = getattr(request.app.state, "mail_worker", None)
        if worker and session.info.get("mail_queued"):
            worker.wake.set()


DB = Annotated[AsyncSession, Depends(transaction, scope="function")]


async def current_user(request: Request, db: DB) -> Account:
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 200:
        raise HTTPException(401, "Please sign in.")
    if not hasattr(request.state, "platform_identity"):
        await load_identity(request, db)
    identity = request.state.platform_identity
    if identity is None:
        raise HTTPException(401, "Your session expired. Please sign in.")
    record, account = identity
    db.info["actor"] = account
    request.state.platform_session = record
    return account


async def load_identity(request: Request, db):
    token = request.cookies.get(COOKIE, "")
    identity = None
    if token and len(token) <= 200:
        identity = (
            await db.execute(
                select(Session, Account)
                .join(Account)
                .where(
                    Session.token_hash == digest(token),
                    Session.expires_at > now(),
                    Session.absolute_expires_at > now(),
                    Account.active.is_(True),
                )
            )
        ).first()
    request.state.platform_identity = identity
    return identity


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


async def revoke_sessions(db, *, user_id=None, token=None):
    from app.platform.events import queue_event

    condition = Session.user_id == user_id if user_id else Session.token_hash == digest(token)
    rows = (await db.execute(delete(Session).where(condition).returning(Session.user_id, Session.id))).all()
    if user_id:
        queue_event(db, [user_id], "session.revoked")
    else:
        for uid, sid in rows:
            queue_event(db, [uid], "session.revoked", session_id=sid)


async def throttle(request, scope: str, identity: str, maximum: int, *, lease=False):
    # Separate committed counters survive failed logins and work across API workers.
    db = get_database(request)
    key = digest(scope + ":" + identity)
    window = int(time.time() // 60)
    # Reserve small blocks against the shared DB ceiling. Local requests consume
    # an already-paid token; workers never mint extra quota. Lost leases only
    # tighten the limit. Login/reset counters always reserve one attempt.
    if not hasattr(request.app.state, "rate_leases"):
        request.app.state.rate_leases = {}
        request.app.state.rate_lease_locks = {}
    leases = request.app.state.rate_leases
    locks = request.app.state.rate_lease_locks
    if len(locks) > 4096:
        request.app.state.rate_lease_locks = locks = {k: v for k, v in locks.items() if v.locked()}
    async with locks.setdefault(key, asyncio.Lock()):
        cached = leases.get(key)
        if lease and cached and cached[0] == window and cached[1] > 0:
            leases[key] = (window, cached[1] - 1)
            return
        granted = False
        for cost in [min(10, maximum), 1] if lease else [1]:
            async with db.engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                count = (
                    await connection.execute(
                        text(
                            "INSERT INTO platform_rate_buckets (key, window_start, attempts) VALUES (:key, :window, :cost) "
                            "ON CONFLICT (key) DO UPDATE SET window_start = :window, attempts = "
                            "CASE WHEN platform_rate_buckets.window_start = :window THEN platform_rate_buckets.attempts + :cost ELSE :cost END "
                            "WHERE platform_rate_buckets.window_start <> :window OR platform_rate_buckets.attempts + :cost <= :maximum "
                            "RETURNING attempts"
                        ),
                        {"key": key, "window": window, "cost": cost, "maximum": maximum},
                    )
                ).scalar()
            if count is not None:
                granted = True
                if lease:
                    if len(leases) >= 2048:
                        leases.clear()
                    leases[key] = (window, cost - 1)
                break
    if not granted:
        raise HTTPException(429, "Too many attempts. Please wait a minute.", headers={"Retry-After": "60"})


def audit(db, user, action, resource=None, detail=""):
    db.add(Audit(actor_id=user.id if user else None, action=action, resource_id=resource, detail=detail))


async def notify(db, recipient, title, href):
    db.add(Notification(user_id=recipient, title=title[:180], href=href))
    from app.platform.events import invalidate

    area = href.split("/")[1]
    paths = ["/notifications"]
    if area != "messages":
        paths.extend(["/" + area, href])
    if area == "agreements":
        paths.append("/preparations")
    if area == "profile":
        paths.append("/auth/me")
    actor = db.info.get("actor")
    recipients = [recipient] + ([actor.id] if actor and actor.role != "admin" else [])
    invalidate(db, recipients, *paths)
    from app.platform.mail import enqueue

    category = area if area in {"messages", "agreements", "cases"} else "account"
    invalidate(db, [recipient], "/dashboard")
    await enqueue(db, recipient, category, title, href, batch=href if area == "messages" else None)


def public_account(user):
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "city": user.city}


async def password_matches(password, stored):
    return await asyncio.to_thread(verify_password, password, stored)
