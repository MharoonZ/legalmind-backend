from fastapi import APIRouter, Depends, Request

from app.platform import admin, agreements, auth, cases, chats, marketplace
from app.platform.security import COOKIE, DB, csrf, load_identity, throttle
from app.platform.realtime import http_router


async def platform_limit(request: Request, db: DB):
    # The unauthenticated session probe is constant-time and touches no database.
    if request.url.path == "/platform/auth/me" and not request.cookies.get(COOKIE):
        return
    public_directory = request.method == "GET" and (
        request.url.path == "/platform/wakeels" or request.url.path.startswith("/platform/wakeels/")
    )
    identity = None if public_directory else await load_identity(request, db)
    owner = identity[1].id if identity else None
    key = "account:" + owner if owner else "ip:" + request.client.host
    await throttle(request, "platform", key, 240, lease=True)


router = APIRouter(prefix="/platform", dependencies=[Depends(csrf), Depends(platform_limit)])
for child in (auth, chats, marketplace, agreements, cases, admin):
    router.include_router(child.router)
router.include_router(http_router)
