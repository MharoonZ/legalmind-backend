from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from app.api.deps import get_database
from app.platform.models import Account, Session, now

from app.platform import admin, agreements, auth, cases, chats, marketplace
from app.platform.security import COOKIE, csrf, digest, throttle


async def platform_limit(request: Request):
    # Persistent across workers. Login has a separate, stricter limit.
    identity = "ip:" + request.client.host
    token = request.cookies.get(COOKIE)
    if token and len(token) <= 200:
        async with get_database(request).session() as session:
            owner = await session.scalar(
                select(Session.user_id)
                .join(Account)
                .where(
                    Session.token_hash == digest(token),
                    Session.expires_at > now(),
                    Session.absolute_expires_at > now(),
                    Account.active.is_(True),
                )
            )
            if owner:
                identity = "account:" + owner
    await throttle(request, "platform", identity, 240)


router = APIRouter(prefix="/platform", dependencies=[Depends(csrf), Depends(platform_limit)])
for child in (auth, chats, marketplace, agreements, cases, admin):
    router.include_router(child.router)
