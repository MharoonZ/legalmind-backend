"""Authenticated WebSockets with transactional PostgreSQL LISTEN/NOTIFY fan-out."""

import asyncio
import json
import logging
import secrets
from collections import OrderedDict, defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, func, select, text

from app.core.config import get_settings
from app.db.session import tls_argument
from app.platform.models import Account, Chat, Message, Session, SocketTicket, now
from app.platform.directory_cache import DirectoryCache
from app.platform.security import DB, User, digest, throttle
from sqlalchemy.orm import aliased

logger = logging.getLogger(__name__)
CHANNEL = "legalmind_platform_events"
http_router = APIRouter(tags=["realtime"])
socket_router = APIRouter()


@dataclass(eq=False)
class Subscriber:
    user_id: str
    session_id: str
    expires_at: datetime
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))


class RealtimeHub:
    def __init__(self, database, settings=None):
        self.database = database
        self.settings = settings
        self.postgres = database.engine.dialect.name == "postgresql"
        self.connected = not self.postgres
        self.clients = defaultdict(set)
        self.inbox = asyncio.Queue(maxsize=1024)
        self.seen = OrderedDict()
        self.listener_task = None
        self.delivery_task = None
        self.title_wakeup = asyncio.Event()
        self.directory_cache = DirectoryCache()

    async def start(self):
        self.delivery_task = asyncio.create_task(self._deliver())
        if self.postgres:
            self.listener_task = asyncio.create_task(self._listen())

    async def stop(self):
        for task in (self.listener_task, self.delivery_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.listener_task, self.delivery_task) if t), return_exceptions=True)
        self._disconnect_all()

    def _disconnect_all(self):
        for clients in list(self.clients.values()):
            for client in list(clients):
                self._put(client, {"type": "reconnect"})

    def _put(self, client, event):
        if client.queue.full():
            while not client.queue.empty():
                client.queue.get_nowait()
            event = {"type": "reconnect"}
        client.queue.put_nowait(event)

    def _received(self, connection, pid, channel, payload):
        try:
            self.inbox.put_nowait(json.loads(payload))
        except (ValueError, asyncio.QueueFull):
            self._disconnect_all()

    async def _listen(self):
        delay = 1
        while True:
            connection = None
            try:
                settings = self.settings or get_settings()
                dsn = self.database.engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
                connection = await asyncpg.connect(
                    dsn, ssl=tls_argument(settings), timeout=settings.db_connect_timeout, statement_cache_size=0
                )
                await connection.add_listener(CHANNEL, self._received)
                disconnected = asyncio.Event()
                connection.add_termination_listener(lambda _, event=disconnected: event.set())
                self.connected = True
                delay = 1
                self.title_wakeup.set()
                logger.info("platform_realtime_ready")
                while not disconnected.is_set():
                    try:
                        await asyncio.wait_for(disconnected.wait(), timeout=45)
                    except TimeoutError:
                        await connection.execute("SELECT 1", timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("platform_realtime_reconnect error=%s", type(error).__name__)
            finally:
                self.connected = False
                self.directory_cache.clear()
                self._disconnect_all()
                if connection:
                    with suppress(Exception):
                        await connection.close(timeout=5)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def publish(self, db, events):
        if self.postgres:
            # IDs and invalidation paths only: no message text or credentials in NOTIFY.
            batches = []
            batch = []
            for event in events:
                if len(json.dumps([*batch, event]).encode()) > 7500 and batch:
                    batches.append(batch)
                    batch = []
                batch.append(event)
            if batch:
                batches.append(batch)
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                [{"channel": CHANNEL, "payload": json.dumps(batch)} for batch in batches],
            )

    async def after_commit(self, events):
        if any(event["type"] == "directory.changed" for event in events):
            self.directory_cache.clear()
        if any(event["type"] == "title.requested" for event in events):
            self.title_wakeup.set()
        if not self.postgres:
            await self.inbox.put(events)

    async def _deliver(self):
        while True:
            events = await self.inbox.get()
            try:
                for event in events:
                    if event["id"] in self.seen:
                        continue
                    self.seen[event["id"]] = None
                    if len(self.seen) > 4096:
                        self.seen.popitem(last=False)
                    if event["type"] == "title.requested":
                        self.title_wakeup.set()
                        continue
                    if event["type"] == "directory.changed":
                        self.directory_cache.clear()
                        for clients in list(self.clients.values()):
                            for client in list(clients):
                                self._put(client, {"id": event["id"], "type": "invalidate", "paths": ["/wakeels"]})
                        continue
                    recipients = [c for uid in event["recipients"] for c in list(self.clients.get(uid, ()))]
                    if not recipients:
                        continue
                    payload = {k: v for k, v in event.items() if k != "recipients"}
                    if event["type"] == "message.created":
                        async with self.database.session() as db:
                            unread_message = aliased(Message)
                            counts = [
                                select(func.count())
                                .select_from(unread_message)
                                .where(
                                    unread_message.conversation_id == Message.conversation_id,
                                    unread_message.sender_id != uid,
                                    unread_message.read_at.is_(None),
                                    unread_message.case_id.is_(None),
                                )
                                .correlate(Message)
                                .scalar_subquery()
                                for uid in event["recipients"]
                            ]
                            row = (
                                await db.execute(select(Message, *counts).where(Message.id == event["message_id"]))
                            ).first()
                            if not row:
                                continue
                            message = row[0]
                            unread = dict(zip(event["recipients"], row[1:], strict=True))
                            payload["message"] = jsonable_encoder(
                                {c.name: getattr(message, c.name) for c in message.__table__.columns}
                            )
                    elif event["type"] == "chat.title":
                        async with self.database.session() as db:
                            chat = await db.get(Chat, event["chat_id"])
                            if not chat:
                                continue
                            payload.update(title=chat.title, title_status=chat.title_status)
                    for client in recipients:
                        if event["type"] == "message.created":
                            self._put(client, {**payload, "unread": unread[client.user_id]})
                            continue
                        if event["type"] == "session.revoked":
                            if event.get("session_id") and event["session_id"] != client.session_id:
                                continue
                        self._put(client, payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("platform_event_delivery_failed error=%s", type(error).__name__)
                self._disconnect_all()


@http_router.post("/realtime-ticket")
async def ticket(request: Request, user: User, db: DB):
    await throttle(request, "socket_ticket", user.id, 30)
    session = request.state.platform_session
    # Expired capabilities never accumulate indefinitely.
    await db.execute(delete(SocketTicket).where(SocketTicket.expires_at < now()))
    token = secrets.token_urlsafe(32)
    db.add(SocketTicket(session_id=session.id, token_hash=digest(token), expires_at=now() + timedelta(seconds=30)))
    return {"ticket": token}


@socket_router.websocket("/platform/ws")
async def websocket(websocket: WebSocket):
    settings = get_settings()
    if websocket.headers.get("origin") not in settings.cors_origins_list:
        await websocket.close(code=1008)
        return
    hub = getattr(websocket.app.state, "realtime", None)
    if not hub or not hub.connected:
        await websocket.close(code=1013)
        return
    await websocket.accept()
    client = None
    tasks = []
    try:
        # The capability travels in the first frame, never in URLs or access logs.
        raw = await asyncio.wait_for(websocket.receive_text(), 5)
        if len(raw) > 1024:
            await websocket.close(code=4401)
            return
        authentication = json.loads(raw)
        if not isinstance(authentication, dict):
            await websocket.close(code=4401)
            return
        token = authentication.get("ticket", "")
        if not isinstance(token, str) or not 30 <= len(token) <= 100:
            await websocket.close(code=4401)
            return
        async with hub.database.session() as db:
            async with db.begin():
                sid = await db.scalar(
                    delete(SocketTicket)
                    .where(SocketTicket.token_hash == digest(token), SocketTicket.expires_at > now())
                    .returning(SocketTicket.session_id)
                )
                identity = (
                    None
                    if not sid
                    else (
                        await db.execute(
                            select(Session, Account)
                            .join(Account)
                            .where(
                                Session.id == sid,
                                Session.expires_at > now(),
                                Session.absolute_expires_at > now(),
                                Account.active.is_(True),
                            )
                            .with_for_update(of=Session)
                        )
                    ).first()
                )
                if identity:
                    session, user = identity
                    if len(hub.clients[user.id]) < 8:
                        client = Subscriber(
                            user.id,
                            session.id,
                            min(session.expires_at, session.absolute_expires_at).replace(tzinfo=UTC),
                        )
                        # Register before releasing the session row lock, so a concurrent
                        # revocation cannot fall between validation and subscription.
                        hub.clients[user.id].add(client)
        if not client:
            await websocket.close(code=4401 if not identity else 4429)
            return
        await websocket.send_json({"type": "ready"})

        async def outgoing():
            while True:
                event = await client.queue.get()
                if event["type"] in {"session.revoked", "reconnect"}:
                    await websocket.close(code=4401 if event["type"] == "session.revoked" else 1013)
                    return
                await websocket.send_json(event)

        async def incoming():
            window = asyncio.get_running_loop().time()
            count = 0
            while True:
                left = (client.expires_at - now()).total_seconds()
                if left <= 0:
                    await websocket.close(code=4401)
                    return
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), min(left, 35))
                except TimeoutError:
                    hub._put(client, {"type": "ping"})
                    continue
                stamp = asyncio.get_running_loop().time()
                if stamp - window >= 60:
                    window, count = stamp, 0
                count += 1
                if len(raw) > 256 or count > 20:
                    await websocket.close(code=1008)
                    return
                frame = json.loads(raw)
                if not isinstance(frame, dict) or frame.get("type") not in {"ping", "pong"}:
                    await websocket.close(code=1008)
                    return
                if frame["type"] == "ping":
                    hub._put(client, {"type": "pong"})

        tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, ValueError, TimeoutError, RuntimeError):
        with suppress(Exception):
            await websocket.close(code=4401)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client:
            hub.clients[client.user_id].discard(client)
            if not hub.clients[client.user_id]:
                hub.clients.pop(client.user_id, None)
