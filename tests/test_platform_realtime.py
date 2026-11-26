"""Realtime authentication, shared quotas, pagination and durable title regressions."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text
from starlette.websockets import WebSocketDisconnect

from app.platform.models import Chat, ChatMessage, SocketTicket, now
from app.platform.security import throttle
from app.platform.titles import TitleWorker, clean_title
from tests.test_platform import engagement, post, register, request
from tests.test_platform import platform as platform_fixture

platform = platform_fixture
ORIGIN = {"origin": "http://localhost:3000"}


def socket_ticket(client, token):
    return post(client, "/realtime-ticket", token, {})["ticket"]


def ready(client, capability):
    connection = client.websocket_connect("/platform/ws", headers=ORIGIN)
    socket = connection.__enter__()
    socket.send_json({"ticket": capability})
    assert socket.receive_json()["type"] == "ready"
    return connection, socket


def until(socket, kind):
    for _ in range(12):
        value = socket.receive_json()
        if value["type"] == kind:
            return value
    raise AssertionError("Expected realtime event was not received")


def test_websocket_message_is_private_deduplicated_and_revoked(platform):
    client = platform
    _, ut, _, wt, _, conversation, _ = engagement(client)
    _, outsider = register(client, suffix="outside")
    tickets = [socket_ticket(client, token) for token in (ut, wt, outsider)]
    from contextlib import ExitStack

    with ExitStack() as stack:
        sockets = []
        for capability in tickets:
            socket = stack.enter_context(client.websocket_connect("/platform/ws", headers=ORIGIN))
            socket.send_json({"ticket": capability})
            assert socket.receive_json()["type"] == "ready"
            sockets.append(socket)
        sender, recipient, observer = sockets
        body = {"body": "Private message for the assigned Wakeel", "request_id": str(uuid4())}
        sent = post(client, f"/conversations/{conversation['id']}/messages", ut, body, 201)
        delivered = until(recipient, "message.created")
        assert delivered["message"]["id"] == sent["id"]
        assert delivered["unread"] == 2  # The engagement fixture already sent one message.
        assert until(sender, "message.created")["message"]["body"] == body["body"]
        duplicate = post(client, f"/conversations/{conversation['id']}/messages", ut, body, 201)
        assert duplicate["id"] == sent["id"]
        for socket in (recipient, observer):
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"
        post(client, "/auth/logout", wt, {})
        with pytest.raises(WebSocketDisconnect) as closed:
            recipient.receive_json()
        assert closed.value.code == 4401


def test_websocket_requires_origin_single_use_ticket_and_existing_session(platform):
    client = platform
    _, token = register(client)
    capability = socket_ticket(client, token)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/platform/ws", headers={"origin": "https://untrusted.example"}):
            pass
    with client.websocket_connect("/platform/ws", headers=ORIGIN) as socket:
        socket.send_json({"ticket": capability})
        assert socket.receive_json()["type"] == "ready"
    with pytest.raises(WebSocketDisconnect) as replay:
        with client.websocket_connect("/platform/ws", headers=ORIGIN) as socket:
            socket.send_json({"ticket": capability})
            socket.receive_json()
    assert replay.value.code == 4401
    capability = socket_ticket(client, token)
    post(client, "/auth/logout", token, {})
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/platform/ws", headers=ORIGIN) as socket:
            socket.send_json({"ticket": capability})
            socket.receive_json()


def test_expired_socket_ticket_is_rejected(platform):
    _, token = register(platform)
    capability = socket_ticket(platform, token)

    async def expire():
        async with platform.app.state.db.session() as db:
            rows = (await db.scalars(select(SocketTicket))).all()
            for row in rows:
                row.expires_at = now() - timedelta(seconds=1)
            await db.commit()

    platform.portal.call(expire)
    with pytest.raises(WebSocketDisconnect):
        with platform.websocket_connect("/platform/ws", headers=ORIGIN) as socket:
            socket.send_json({"ticket": capability})
            socket.receive_json()


def test_guest_session_probe_does_not_query_database(platform):
    queries = []
    engine = platform.app.state.db.engine.sync_engine

    def record(*args):
        queries.append(args[2])

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert request(platform, "GET", "/auth/me").status_code == 401
        assert queries == []
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_rate_leases_never_exceed_shared_database_limit(platform, monkeypatch):
    monkeypatch.setattr("app.platform.security.time.time", lambda: 123456)
    database = platform.app.state.db
    requests = [SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=database))) for _ in range(3)]

    async def exercise():
        for req in requests:
            await throttle(req, "lease_test", "shared", 21, lease=True)
        for req in requests[:2]:
            for _ in range(9):
                await throttle(req, "lease_test", "shared", 21, lease=True)
        for req in requests:
            with pytest.raises(HTTPException) as blocked:
                await throttle(req, "lease_test", "shared", 21, lease=True)
            assert blocked.value.status_code == 429
        async with database.session() as db:
            assert await db.scalar(text("select max(attempts) from platform_rate_buckets")) == 21

    platform.portal.call(exercise)


def test_six_character_registration_and_password_change(platform):
    registration = {"email": "six@example.test", "name": "Six Character", "password": "abc123", "role": "user"}
    result = request(platform, "POST", "/auth/register", json=registration)
    assert result.status_code == 201
    token = result.cookies["legalmind_session"]
    assert (
        request(
            platform,
            "POST",
            "/auth/register",
            json={**registration, "email": "short@example.test", "password": "abc12"},
        ).status_code
        == 422
    )
    result = request(
        platform, "POST", "/auth/password", token, json={"current_password": "abc123", "password": "xyz456"}
    )
    assert result.status_code == 200
    assert (
        request(
            platform,
            "POST",
            "/auth/password",
            result.cookies["legalmind_session"],
            json={"current_password": "xyz456", "password": "short"},
        ).status_code
        == 422
    )
    assert (
        request(
            platform, "POST", "/auth/login", json={"email": registration["email"], "password": "xyz456"}
        ).status_code
        == 200
    )


def test_chat_history_paginates_without_losing_equal_timestamp_rows(platform):
    user, token = register(platform)
    chat = post(platform, "/chats", token, {}, 201)

    async def populate():
        async with platform.app.state.db.session() as db:
            stamp = now()
            for index in range(101):
                db.add(
                    ChatMessage(
                        chat_id=chat["id"],
                        request_id=str(uuid4()),
                        question=str(index),
                        response={"answer": "Fixture"},
                        created_at=stamp,
                    )
                )
            await db.commit()

    platform.portal.call(populate)
    first = request(platform, "GET", "/chats/" + chat["id"], token).json()
    second = request(platform, "GET", "/chats/" + chat["id"] + "?before=" + first["messages"][0]["id"], token).json()
    third = request(platform, "GET", "/chats/" + chat["id"] + "?before=" + second["messages"][0]["id"], token).json()
    assert [len(page["messages"]) for page in (first, second, third)] == [50, 50, 1]
    assert len({m["id"] for page in (first, second, third) for m in page["messages"]}) == 101


class TitleModel:
    configured = True
    provider = "gemini"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def generate_json(self, system, prompt, schema, **options):
        self.calls.append((prompt, options))
        if self.fail:
            raise TimeoutError()
        return {"title": "Consumer Receipt Rights"}


def test_title_is_generated_once_without_changing_the_answer(platform):
    _, token = register(platform)
    chat = post(platform, "/chats", token, {}, 201)
    question = "The shopkeeper refused a receipt. What are my rights?"
    answer = post(platform, "/chats/" + chat["id"] + "/ask", token, {"question": question, "request_id": str(uuid4())})
    assert answer["question"] == question
    model = TitleModel()
    worker = TitleWorker(platform.app.state.db, model, platform.app.state.realtime)
    assert platform.portal.call(worker.run_one)
    assert not platform.portal.call(worker.run_one)
    saved = request(platform, "GET", "/chats/" + chat["id"], token).json()
    assert saved["title"] == "Consumer Receipt Rights"
    assert saved["title_status"] == "ready"
    assert saved["messages"][0]["response"] == answer["response"]
    assert len(model.calls) == 1
    assert model.calls[0][1]["allow_failover"] is False
    assert model.calls[0][1]["timeout"] == 8


def test_title_outage_has_bounded_retries_and_never_copies_question(platform):
    _, token = register(platform)
    chat = post(platform, "/chats", token, {}, 201)
    post(
        platform,
        "/chats/" + chat["id"] + "/ask",
        token,
        {"question": "Where are my rights?", "request_id": str(uuid4())},
    )
    model = TitleModel(fail=True)
    worker = TitleWorker(platform.app.state.db, model, platform.app.state.realtime)

    async def retry():
        for _ in range(3):
            assert await worker.run_one()
            async with platform.app.state.db.session() as db:
                row = await db.get(Chat, chat["id"])
                row.title_attempted_at = now() - timedelta(seconds=61)
                await db.commit()
        assert not await worker.run_one()

    platform.portal.call(retry)
    saved = request(platform, "GET", "/chats/" + chat["id"], token).json()
    assert saved["title"] == "Legal conversation" and saved["title_status"] == "failed"
    assert len(model.calls) == 3
    assert post(platform, "/chats/" + chat["id"] + "/title", token, {})["title_status"] == "pending"


@pytest.mark.parametrize("title", ["", "a" * 73, "What are my rights?"])
def test_title_rejects_empty_long_or_copied_output(title):
    with pytest.raises(ValueError):
        clean_title(title, "What are my rights?")


def test_crashed_final_title_attempt_becomes_retryable_failure(platform):
    _, token = register(platform)
    chat = post(platform, "/chats", token, {}, 201)

    async def recover():
        async with platform.app.state.db.session() as db:
            row = await db.get(Chat, chat["id"])
            row.title_status = "generating"
            row.title_attempts = 3
            row.title_attempted_at = now() - timedelta(minutes=3)
            await db.commit()
        worker = TitleWorker(platform.app.state.db, TitleModel(), platform.app.state.realtime)
        assert not await worker.run_one()

    platform.portal.call(recover)
    assert request(platform, "GET", "/chats/" + chat["id"], token).json()["title_status"] == "failed"


def test_directory_cache_invalidates_on_profile_and_access_changes(platform):
    _, _, wakeel, wt, at, _, _ = engagement(platform)
    path = "/wakeels"
    assert request(platform, "GET", path).json()[0]["name"] == wakeel["name"]
    statements = []

    def record(*args):
        statements.append(args[2])

    event.listen(platform.app.state.db.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert request(platform, "GET", path).status_code == 200
    finally:
        event.remove(platform.app.state.db.engine.sync_engine, "before_cursor_execute", record)
    assert not any("platform_wakeels" in sql for sql in statements)
    post(platform, "/auth/profile", wt, {"name": "Updated Professional", "city": "Karachi"})
    assert request(platform, "GET", path).json()[0]["name"] == "Updated Professional"
    post(
        platform,
        "/admin/actions",
        at,
        {"action": "suspend", "resource_id": wakeel["id"], "reason": "Fixture suspension"},
    )
    assert request(platform, "GET", path).json() == []


def test_directory_cache_does_not_store_inflight_result_after_invalidation():
    import asyncio

    async def check():
        from app.platform.directory_cache import DirectoryCache

        cache = DirectoryCache()

        async def stale():
            cache.clear()
            return ["stale"]

        assert await cache.get("key", stale, enabled=lambda: True) == ["stale"]
        assert not cache.values

        async def current():
            return ["current"]

        assert await cache.get("key", current, enabled=lambda: True) == ["current"]

        async def disconnected():
            return ["fresh without listener"]

        assert await cache.get("key", disconnected, enabled=lambda: False) == ["fresh without listener"]

    asyncio.run(check())
