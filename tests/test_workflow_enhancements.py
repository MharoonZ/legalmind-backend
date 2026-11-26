"""Focused regressions for recovery, queued email and independent legal records."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.platform.mail import MailWorker, otp_value
from app.platform.models import Agreement, MailOutbox, OtpChallenge, now
from app.platform.workflows import expire_agreements
from tests.test_platform import action, engagement, post, register, request, sign, terms
from tests.test_platform import platform as platform_fixture

platform = platform_fixture


@pytest.fixture
def email_settings(monkeypatch):
    settings = get_settings()
    for name, value in {
        "smtp_user": "test@example.test",
        "smtp_password": "test-only-password",
        "smtp_from_email": "test@example.test",
        "otp_secret": "test-only-otp-secret-at-least-32-characters",
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


def test_otp_is_single_use_and_reset_revokes_sessions(platform, email_settings):
    user, token = register(platform)
    result = post(platform, "/auth/forgot-password", None, {"email": user["email"]})
    code = otp_value(result["challenge_id"], user["id"])

    async def inspect():
        async with platform.app.state.db.session() as db:
            row = await db.get(OtpChallenge, result["challenge_id"])
            assert row.otp_hash != code and len(row.otp_hash) == 64
            assert (row.expires_at - row.created_at).total_seconds() == pytest.approx(300, abs=0.1)
            jobs = (await db.scalars(select(MailOutbox).where(MailOutbox.category == "otp"))).all()
            assert len(jobs) == 1 and code not in str(jobs[0].payload)

    platform.portal.call(inspect)
    body = {"email": user["email"], "challenge_id": result["challenge_id"], "otp": code}
    post(platform, "/auth/verify-otp", None, {**body, "email": "nobody@example.test"}, 400)
    reset = post(platform, "/auth/verify-otp", None, body)
    post(platform, "/auth/verify-otp", None, body, 400)
    post(platform, "/auth/reset", None, {"token": reset["token"], "password": "short"}, 422)
    post(platform, "/auth/reset", None, {"token": reset["token"], "password": "new-password"})
    assert request(platform, "GET", "/auth/me", token).status_code == 401
    post(platform, "/auth/reset", None, {"token": reset["token"], "password": "another-password"}, 400)
    post(platform, "/auth/login", None, {"email": user["email"], "password": "new-password"})


@pytest.mark.parametrize("failure", ["expired", "locked"])
def test_otp_expiry_and_failed_attempts_persist(platform, email_settings, failure):
    user, _ = register(platform)
    result = post(platform, "/auth/forgot-password", None, {"email": user["email"]})
    code = otp_value(result["challenge_id"], user["id"])
    body = {"email": user["email"], "challenge_id": result["challenge_id"], "otp": code}
    if failure == "expired":

        async def expire():
            async with platform.app.state.db.session() as db, db.begin():
                row = await db.get(OtpChallenge, result["challenge_id"])
                row.expires_at = now() - timedelta(seconds=1)

        platform.portal.call(expire)
    else:
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            post(platform, "/auth/verify-otp", None, {**body, "otp": wrong}, 400)
    post(platform, "/auth/verify-otp", None, body, 400)


def test_otp_resend_cooldown_invalidates_old_code(platform, email_settings):
    user, _ = register(platform)
    first = post(platform, "/auth/forgot-password", None, {"email": user["email"]})
    post(platform, "/auth/forgot-password", None, {"email": user["email"]}, 429)

    async def age():
        async with platform.app.state.db.session() as db, db.begin():
            row = await db.get(OtpChallenge, first["challenge_id"])
            row.created_at = now() - timedelta(seconds=61)
            from app.platform.security import digest

            await db.execute(
                text("UPDATE platform_rate_buckets SET window_start=0 WHERE key=:key"),
                {"key": digest("otp_cooldown:" + user["email"])},
            )
            assert len((await db.scalars(select(MailOutbox).where(MailOutbox.category == "otp"))).all()) == 1

    platform.portal.call(age)
    latest = post(platform, "/auth/forgot-password", None, {"email": user["email"]})
    post(
        platform,
        "/auth/verify-otp",
        None,
        {
            "email": user["email"],
            "challenge_id": first["challenge_id"],
            "otp": otp_value(first["challenge_id"], user["id"]),
        },
        400,
    )
    post(
        platform,
        "/auth/verify-otp",
        None,
        {
            "email": user["email"],
            "challenge_id": latest["challenge_id"],
            "otp": otp_value(latest["challenge_id"], user["id"]),
        },
    )


def test_email_batch_preferences_and_retry(platform, email_settings, monkeypatch):
    from app.platform import mail

    user, ut, wakeel, wt, _, conv, _ = engagement(platform)
    for _ in range(2):
        post(
            platform,
            f"/conversations/{conv['id']}/messages",
            ut,
            {"body": "Private evidence", "request_id": str(uuid4())},
            201,
        )

    async def inspect():
        async with platform.app.state.db.session() as db, db.begin():
            jobs = (await db.scalars(select(MailOutbox).where(MailOutbox.category == "messages"))).all()
            assert len(jobs) == 1
            assert "Private evidence" not in str(jobs[0].payload)
            for row in (await db.scalars(select(MailOutbox))).all():
                row.status = "skipped"
            jobs[0].status, jobs[0].available_at = "pending", now() - timedelta(seconds=1)
            return jobs[0].id

    identifier = platform.portal.call(inspect)
    worker = MailWorker(platform.app.state.db, platform.app.state.realtime, email_settings)

    def failure(*args):
        raise TimeoutError("test SMTP outage")

    monkeypatch.setattr(mail, "deliver", failure)
    assert platform.portal.call(worker.send_one)

    async def retry():
        async with platform.app.state.db.session() as db, db.begin():
            row = await db.get(MailOutbox, identifier)
            assert row.status == "pending" and row.attempts == 1
            row.available_at = now() - timedelta(seconds=1)

    platform.portal.call(retry)
    delivered = []
    monkeypatch.setattr(mail, "deliver", lambda message, settings: delivered.append(message))
    assert platform.portal.call(worker.send_one)
    assert len(delivered) == 1 and "Private evidence" not in delivered[0].as_string()
    assert not platform.portal.call(worker.send_one)
    post(platform, "/auth/email-preferences", wt, {"messages": False})
    post(platform, f"/conversations/{conv['id']}/messages", ut, {"body": "One more", "request_id": str(uuid4())}, 201)

    async def count():
        async with platform.app.state.db.session() as db:
            assert len((await db.scalars(select(MailOutbox).where(MailOutbox.category == "messages"))).all()) == 1

    platform.portal.call(count)


def test_independent_cases_linking_drafts_and_permissions(platform):
    user, ut, wakeel, wt, _, conv, contract = engagement(platform)
    other, ot = register(platform, suffix="outside")
    case = post(
        platform, "/cases", ut, {"title": "Independent case", "description": "Legal work without a contract"}, 201
    )
    assert case["agreement_id"] is None and case["status"] == "DRAFT"
    assert request(platform, "GET", f"/cases/{case['id']}", ot).status_code == 404
    post(
        platform,
        f"/cases/{case['id']}/assignment",
        ut,
        {"wakeel_id": wakeel["id"], "reason": "Client selected the Wakeel"},
    )
    contract = action(platform, contract, wt, "send")
    post(platform, f"/cases/{case['id']}/agreements", ut, {"agreement_id": contract["id"]})
    post(platform, f"/cases/{case['id']}/updates", wt, {"status": "IN_PROGRESS", "note": "Work has started"})
    assert len(request(platform, "GET", f"/agreements/{contract['id']}", ut).json()["cases"]) == 1
    post(platform, f"/cases/{case['id']}/updates", ut, {"status": "CLOSED", "note": "Unauthorized change"}, 403)
    draft = post(platform, f"/agreement-drafts/{conv['id']}", wt, {"revision": 0, "terms": {"title": "Resume me"}})
    assert draft["revision"] == 1
    post(platform, f"/agreement-drafts/{conv['id']}", wt, {"revision": 0, "terms": {}}, 409)
    assert request(platform, "GET", f"/agreement-drafts/{conv['id']}", ut).status_code == 403
    second = post(
        platform, "/agreements", wt, {"conversation_id": conv["id"], "case_id": case["id"], "terms": terms()}, 201
    )
    assert request(platform, "GET", f"/agreement-drafts/{conv['id']}", wt).json()["revision"] == 0
    assert len(request(platform, "GET", f"/cases/{case['id']}", ut).json()["agreements"]) == 1  # private draft hidden
    action(platform, second, wt, "send")
    assert len(request(platform, "GET", f"/cases/{case['id']}", ut).json()["agreements"]) == 2
    foreign = post(
        platform,
        "/cases",
        ot,
        {"title": "Other case", "description": "Another person's matter", "wakeel_id": wakeel["id"]},
        201,
    )
    post(platform, f"/cases/{foreign['id']}/agreements", wt, {"agreement_id": contract["id"]}, 409)


@pytest.mark.parametrize("end", ["expire", "terminate"])
def test_ending_agreement_does_not_close_case(platform, end):
    user, ut, wakeel, wt, _, _, contract = engagement(platform)
    contract = action(platform, contract, wt, "send")
    contract = action(platform, contract, ut, "accept")
    contract = sign(platform, contract, user, ut)
    contract = sign(platform, contract, wakeel, wt)
    case = post(
        platform,
        "/cases",
        wt,
        {"agreement_id": contract["id"], "title": "Ongoing case", "description": "Still in progress"},
        201,
    )
    if end == "terminate":
        contract = action(platform, contract, ut, "request_termination", reason="Mutual termination requested")
        contract = action(platform, contract, wt, "confirm_termination", reason="Agreed to terminate")
        assert contract["status"] == "TERMINATED"
    else:

        async def expire():
            async with platform.app.state.db.session() as db, db.begin():
                row = await db.get(Agreement, contract["id"])
                row.expires_on = "2020-01-01"
            await expire_agreements(platform.app.state.db, platform.app.state.realtime)

        platform.portal.call(expire)
        contract = request(platform, "GET", f"/agreements/{contract['id']}", ut).json()
        assert contract["status"] == "EXPIRED"
    action(platform, contract, ut, "archive", reason="Preserve the final agreement")
    assert request(platform, "GET", f"/cases/{case['id']}", ut).json()["status"] == "ASSIGNED"
