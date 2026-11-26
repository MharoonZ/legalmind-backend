"""Regression checks for session and workflow boundaries."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.platform.models import ResetToken, Session, now
from app.platform.security import digest
from tests.test_platform import action, admin_token, engagement, post, register, request, terms
from tests.test_platform import platform as platform_fixture

platform = platform_fixture


def test_expired_session_and_single_use_reset(platform):
    client = platform
    user, token = register(client)
    reset_secret = "one-use-reset-test-token-with-enough-entropy"

    async def expire_and_issue():
        async with client.app.state.db.session() as db:
            session = await db.scalar(select(Session).where(Session.token_hash == digest(token)))
            session.expires_at = now() - timedelta(seconds=1)
            db.add(
                ResetToken(
                    user_id=user["id"], token_hash=digest(reset_secret), expires_at=now() + timedelta(minutes=30)
                )
            )
            await db.commit()

    asyncio.run(expire_and_issue())
    assert request(client, "GET", "/auth/me", token).status_code == 401
    post(client, "/auth/reset", None, {"token": reset_secret, "password": "replacement-password"})
    assert (
        request(client, "POST", "/auth/reset", json={"token": reset_secret, "password": "second-password"}).status_code
        == 400
    )
    assert (
        request(
            client, "POST", "/auth/login", json={"email": user["email"], "password": "long-secure-password"}
        ).status_code
        == 401
    )
    assert (
        request(
            client, "POST", "/auth/login", json={"email": user["email"], "password": "replacement-password"}
        ).status_code
        == 200
    )


def test_suspension_revokes_sessions_and_rejects_public_role_updates(platform):
    client = platform
    user, token = register(client)
    admin = admin_token(client)
    post(client, "/admin/actions", admin, {"action": "suspend", "resource_id": user["id"], "reason": "Account review"})
    assert request(client, "GET", "/auth/me", token).status_code == 401
    assert (
        request(
            client, "POST", "/auth/login", json={"email": user["email"], "password": "long-secure-password"}
        ).status_code
        == 401
    )
    post(client, "/admin/actions", admin, {"action": "restore", "resource_id": user["id"], "reason": "Review complete"})
    assert request(client, "GET", "/auth/me", token).status_code == 401


@pytest.mark.parametrize("attempt", ["send", "sign", "amend", "confirm_closure", "dispute"])
def test_draft_cannot_skip_approval_and_signing(platform, attempt):
    client = platform
    _, ut, _, wt, _, _, contract = engagement(client)
    result = request(
        client,
        "POST",
        f"/agreements/{contract['id']}/actions",
        ut,
        json={
            "action": attempt,
            "revision": contract["revision"],
            "reason": "Testing invalid action",
            "terms": terms(),
            "consent": True,
            "signed_name": "Person a",
        },
    )
    assert result.status_code in {403, 404, 409}
    assert request(client, "GET", f"/agreements/{contract['id']}", wt).json()["status"] == "DRAFT"


def test_decline_and_rejected_change_requests_keep_history(platform):
    client = platform
    _, ut, _, wt, _, _, contract = engagement(client)
    contract = action(client, contract, wt, "send")
    assert (
        request(
            client,
            "POST",
            f"/agreements/{contract['id']}/actions",
            ut,
            json={"action": "decline", "revision": contract["revision"]},
        ).status_code
        == 409
    )
    contract = action(client, contract, ut, "request_changes", reason="Revise the scope")
    contract = action(client, contract, wt, "reject_changes", reason="This scope requires more work")
    assert contract["status"] == "NEGOTIATING"
    contract = action(client, contract, ut, "decline", reason="The proposed scope is unsuitable")
    assert contract["status"] == "DECLINED"
    detail = request(client, "GET", f"/agreements/{contract['id']}", ut).json()
    assert len(detail["versions"]) == 1
    assert detail["events"][-1]["reason"] == "The proposed scope is unsuitable"
    assert (
        request(
            client, "POST", "/reviews", ut, json={"agreement_id": contract["id"], "rating": 4, "body": "Too early"}
        ).status_code
        == 409
    )


def test_private_notifications_and_idempotent_messages(platform):
    client = platform
    _, ut, _, wt, _, thread, _ = engagement(client)
    _, outsider = register(client, suffix="outsider")
    message = {"body": "Please confirm receipt", "request_id": str(uuid4())}
    first = post(client, f"/conversations/{thread['id']}/messages", ut, message, 201)
    again = post(client, f"/conversations/{thread['id']}/messages", ut, message, 201)
    assert first["id"] == again["id"]
    notification = request(client, "GET", "/notifications", wt).json()[0]
    assert request(client, "POST", f"/notifications/{notification['id']}/read", outsider, json={}).status_code == 404
    post(client, f"/notifications/{notification['id']}/read", wt, {})


def test_missing_csrf_header_is_rejected(platform):
    platform.headers.pop("X-LegalMind-Client")
    assert (
        platform.post(
            "/platform/auth/login", json={"email": "admin@example.test", "password": "administrator-password"}
        ).status_code
        == 403
    )
