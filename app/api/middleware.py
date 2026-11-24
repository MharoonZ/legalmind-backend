"""ASGI middleware: request ids, access logs, body limits and the local-only gate."""
from __future__ import annotations

import logging
import re
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.deps import require_local_development
from app.core.config import get_settings
from app.core.logging import request_id

logger = logging.getLogger("app.access")

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_LOCAL_PREFIXES = ("/local-admin", "/contracts")
_UPLOAD_PREFIXES = ("/local-admin/legal/uploads", "/contracts")
_JSON_LIMIT = 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class RequestContextMiddleware:
    """Assigns a request id, enforces body limits, and writes one access-log line.

    Local administration paths are rejected here, before any upload is read,
    unless the request is a loopback development request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        headers = dict(scope.get("headers") or ())
        supplied = headers.get(b"x-request-id", b"").decode("latin-1")
        identifier = supplied if _SAFE_ID.fullmatch(supplied) else uuid4().hex[:16]
        token = request_id.set(identifier)
        path = scope.get("path", "")
        started = perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                extra = [(b"x-request-id", identifier.encode()), (b"x-content-type-options", b"nosniff"),
                         (b"referrer-policy", b"no-referrer")]
                if not any(name.lower() == b"cache-control" for name, _ in message.get("headers", ())):
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message.get("headers", ()), *extra]}
            await send(message)

        async def reject(code: int, detail: str):
            await JSONResponse({"detail": detail}, status_code=code)(scope, receive, send_wrapper)

        try:
            if path.startswith(_LOCAL_PREFIXES):
                try:
                    require_local_development(Request(scope), settings)
                except HTTPException as exc:
                    await reject(exc.status_code, str(exc.detail))
                    return
            limit = settings.max_request_bytes if path.startswith(_UPLOAD_PREFIXES) else _JSON_LIMIT
            declared = headers.get(b"content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                await reject(413, "Request body is too large.")
                return
            received = 0

            async def receive_wrapper():
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _BodyTooLarge()
                return message

            try:
                await self.app(scope, receive_wrapper, send_wrapper)
            except _BodyTooLarge:
                await reject(413, "Request body is too large.")
        finally:
            if path not in {"/health", "/health/live", "/health/ready"}:
                logger.info("%s %s -> %d in %.0f ms", scope.get("method"), path, status_code,
                            (perf_counter() - started) * 1000)
            request_id.reset(token)
