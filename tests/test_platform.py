"""Exercise the actual platform HTTP routes with an isolated SQL database."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import create_app
from app.platform.models import Account, Base, Chat
from app.platform.security import hash_password
from app.schemas.qa import AskOut


class TestDatabase:
    __test__ = False

    def __init__(self, url):
        self.engine = create_async_engine(url)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self):
        async with self.factory() as session:
            yield session


class TestQA:
    __test__ = False

    async def ask(self, payload, *, record=True):
        assert record is False
        return AskOut(
            status="answered", question=payload.question, answer="Answer with context: " + str(len(payload.history))
        ), {}


@pytest.fixture
def platform(tmp_path):
    app = create_app()
    database = TestDatabase("sqlite+aiosqlite:///" + str(tmp_path / "platform.db"))
    tables = [t for t in Base.metadata.sorted_tables if t.name.startswith("platform_")]

    @asynccontextmanager
    async def lifespan(app):
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        async with database.session() as db:
            db.add(
                Account(
                    email="admin@example.test",
                    name="Administrator",
                    role="admin",
                    password_hash=hash_password("administrator-password"),
                )
            )
            await db.commit()
        app.state.db = database
        app.state.services = SimpleNamespace(qa=TestQA(), ask_slots=asyncio.Semaphore(2))
        yield
        await database.engine.dispose()

    app.router.lifespan_context = lifespan
    from app.core.config import get_settings

    original = get_settings()
    app.dependency_overrides[get_settings] = lambda: original.model_copy(update={"legal_search_enabled": True})
    with TestClient(app) as client:
        client.headers["X-LegalMind-Client"] = "web"
        yield client


def request(client, method, path, token=None, **kwargs):
    client.cookies.clear()
    headers = {"Cookie": "legalmind_session=" + token} if token else {}
    return client.request(method, "/platform" + path, headers=headers, **kwargs)


def register(client, role="user", suffix="a"):
    result = request(
        client,
        "POST",
        "/auth/register",
        json={
            "email": suffix + "@example.test",
            "password": "long-secure-password",
            "name": "Person " + suffix,
            "role": role,
            "bar_number": "BAR-" + suffix,
            "specialization": "Consumer",
            "city": "Lahore",
        },
    )
    assert result.status_code == 201, result.text
    return result.json(), result.cookies["legalmind_session"]


def admin_token(client):
    result = request(
        client, "POST", "/auth/login", json={"email": "admin@example.test", "password": "administrator-password"}
    )
    assert result.status_code == 200, result.text
    return result.cookies["legalmind_session"]


def post(client, path, token, body, status=200):
    result = request(client, "POST", path, token, json=body)
    assert result.status_code == status, result.text
    return result.json()


def terms():
    return {
        "title": "Preparation of documents",
        "description": "Prepare consumer complaint documents",
        "scope": "Review the evidence",
        "deliverables": "Written complaint",
        "fees": "5000.00",
        "currency": "PKR",
        "payment_terms": "On completion",
        "start_date": "2026-12-01",
        "completion_date": "2026-12-20",
        "user_responsibilities": "Provide evidence",
        "wakeel_responsibilities": "Prepare documents",
        "conditions": "Subject to agreed scope",
        "required_documents": "Receipt",
    }


def engagement(client):
    user, ut = register(client)
    wakeel, wt = register(client, "wakeel", "w")
    at = admin_token(client)
    assert request(client, "GET", "/wakeels").json() == []
    post(
        client,
        "/admin/actions",
        at,
        {"action": "verify", "resource_id": wakeel["id"], "reason": "Bar registration checked"},
    )
    assert len(request(client, "GET", "/wakeels").json()) == 1
    conv = post(client, "/conversations", ut, {"wakeel_id": wakeel["id"]}, 201)
    post(
        client,
        f"/conversations/{conv['id']}/messages",
        ut,
        {"body": "Please prepare my documents", "request_id": str(uuid4())},
        201,
    )
    prep = post(
        client,
        "/preparations",
        ut,
        {"conversation_id": conv["id"], "title": "Prepare complaint", "requirements": "Prepare a consumer complaint"},
        201,
    )
    body = {"conversation_id": conv["id"], "preparation_id": prep["id"], "terms": terms()}
    assert request(client, "POST", "/agreements", ut, json=body).status_code == 403
    contract = post(client, "/agreements", wt, body, 201)
    return user, ut, wakeel, wt, at, conv, contract


def action(client, contract, token, name, **extra):
    return post(
        client,
        f"/agreements/{contract['id']}/actions",
        token,
        {"action": name, "revision": contract["revision"], **extra},
    )


def sign(client, contract, user, token):
    detail = request(client, "GET", f"/agreements/{contract['id']}", token).json()
    version = next(v for v in detail["versions"] if v["number"] == contract["current_version"])
    return action(
        client, contract, token, "sign", consent=True, signed_name=user["name"], version_digest=version["digest"]
    )


def test_complete_workflow_and_ownership(platform):
    client = platform
    user, ut, wakeel, wt, at, conv, contract = engagement(client)
    outsider, ot = register(client, suffix="outsider")
    for path in (f"/conversations/{conv['id']}", f"/agreements/{contract['id']}"):
        assert request(client, "GET", path, ot).status_code == 404
    assert request(client, "GET", "/admin", ut).status_code == 403
    assert (
        request(
            client, "POST", f"/agreements/{contract['id']}/actions", ut, json={"action": "decline", "revision": 1}
        ).status_code
        == 404
    )
    contract = action(client, contract, wt, "send")
    contract = action(client, contract, ut, "request_changes", reason="Please reduce the fees")
    old = contract.copy()
    contract = action(
        client, contract, wt, "revise", reason="Fees reduced as requested", terms={**terms(), "fees": "4000.00"}
    )
    assert (
        request(
            client,
            "POST",
            f"/agreements/{contract['id']}/actions",
            ut,
            json={"action": "accept", "revision": old["revision"]},
        ).status_code
        == 409
    )
    contract = action(client, contract, ut, "accept")
    contract = sign(client, contract, user, ut)
    assert contract["status"] == "SIGNING_PENDING"
    contract = sign(client, contract, wakeel, wt)
    assert contract["status"] == "ACTIVE"
    contract = action(
        client, contract, wt, "amend", reason="Additional deliverable requested", terms={**terms(), "fees": "4500.00"}
    )
    assert contract["active_version"] == 2 and contract["current_version"] == 3
    contract = action(client, contract, ut, "accept")
    contract = sign(client, contract, user, ut)
    contract = sign(client, contract, wakeel, wt)
    assert contract["active_version"] == 3
    case = post(
        client,
        "/cases",
        wt,
        {"agreement_id": contract["id"], "title": "Consumer claim", "description": "Prepare the claim"},
        201,
    )
    post(client, f"/cases/{case['id']}/updates", wt, {"note": "Documents received", "status": "IN_PROGRESS"})
    assert request(client, "GET", f"/cases/{case['id']}", ot).status_code == 404
    attachment = request(
        client,
        "POST",
        f"/cases/{case['id']}/attachments",
        ut,
        files={"file": ("receipt.pdf", b"%PDF-1.4\nexample", "application/pdf")},
    )
    assert attachment.status_code == 201, attachment.text
    file_path = f"/cases/{case['id']}/attachments/{attachment.json()['id']}"
    assert request(client, "GET", file_path, ot).status_code == 404
    assert request(client, "GET", file_path, wt).content.startswith(b"%PDF")
    contract = action(client, contract, ut, "request_closure")
    contract = action(client, contract, wt, "dispute", reason="Work remains to be completed")
    post(
        client,
        "/admin/actions",
        at,
        {
            "action": "resolve_dispute",
            "resource_id": contract["id"],
            "reason": "Parties agreed to continue",
            "outcome": "ACTIVE",
        },
    )
    contract = request(client, "GET", f"/agreements/{contract['id']}", ut).json()
    contract = action(client, contract, wt, "request_closure")
    contract = action(client, contract, ut, "confirm_closure")
    assert contract["status"] == "COMPLETED"
    post(client, "/reviews", ut, {"agreement_id": contract["id"], "rating": 5, "body": "Helpful service"}, 201)
    assert (
        request(
            client, "POST", "/reviews", ut, json={"agreement_id": contract["id"], "rating": 5, "body": "Duplicate"}
        ).status_code
        == 409
    )
    detail = request(client, "GET", f"/agreements/{contract['id']}", ut).json()
    assert len(detail["versions"]) == 3 and len(detail["acceptances"]) == 6
    assert request(client, "GET", "/notifications", ut).json()
    assert request(client, "GET", "/admin", at).json()["audit"]


def test_auth_private_chats_logout_and_csrf(platform):
    client = platform
    user, token = register(client)
    other, ot = register(client, suffix="other")
    assert (
        request(
            client,
            "POST",
            "/auth/register",
            json={"name": "Admin", "email": "evil@example.test", "password": "long-password", "role": "admin"},
        ).status_code
        == 422
    )
    chat = post(client, "/chats", token, {}, 201)
    body = {"question": "What does section 3 say?", "request_id": str(uuid4())}
    first = post(client, f"/chats/{chat['id']}/ask", token, body)
    same = post(client, f"/chats/{chat['id']}/ask", token, body)
    assert first["id"] == same["id"]
    second = post(
        client, f"/chats/{chat['id']}/ask", token, {"question": "What is its punishment?", "request_id": str(uuid4())}
    )
    assert second["response"]["answer"].endswith("2")
    assert request(client, "GET", f"/chats/{chat['id']}", ot).status_code == 404
    assert request(client, "POST", f"/chats/{chat['id']}/ask", ot, json=body).status_code == 404
    client.cookies.clear()
    response = client.post(
        "/platform/auth/logout", headers={"Cookie": "legalmind_session=" + token, "Origin": "https://evil.test"}
    )
    assert response.status_code == 403
    post(client, "/auth/logout", token, {})
    assert request(client, "GET", "/chats", token).status_code == 401
    response = request(client, "POST", "/auth/login", json={"email": user["email"], "password": "long-secure-password"})
    renewed = response.cookies["legalmind_session"]
    assert len(request(client, "GET", "/chats", renewed).json()) == 1
    assert len(request(client, "GET", f"/chats/{chat['id']}", renewed).json()["messages"]) == 2
    post(
        client,
        "/auth/password",
        renewed,
        {"current_password": "long-secure-password", "password": "a-new-long-password"},
    )
    assert request(client, "GET", "/auth/me", renewed).status_code == 401
    assert request(client, "POST", "/auth/profile", ot, json={"name": "Other", "role": "admin"}).status_code == 422


def test_guest_ask_is_public_and_not_saved(platform):
    response = platform.post("/ask", json={"question": "Hello"})
    assert response.status_code == 200

    async def count():
        async with platform.app.state.db.session() as db:
            return list((await db.scalars(select(Chat))).all())

    assert asyncio.run(count()) == []


def test_model_alias_keeps_distinct_fallback():
    from app.qa.llm import AnswerGenerator
    from tests.test_qa_llm import config

    generator = AnswerGenerator(
        config(gemini_model="same", gemini_fallback_model="same", gemini_second_fallback_model="different")
    )
    assert generator._gemini_models() == ["same", "different"]
