"""Isolated browser-test server. Never loads development credentials or live data."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.update(
    DATABASE_URL="postgresql://test:test@127.0.0.1:1/test",
    DB_SSL="false",
    LEGAL_SEARCH_ENABLED="true",
    MODELS_WARM_ON_STARTUP="false",
    API_KEYS="",
    CORS_ORIGINS="http://localhost:3011",
    GEMINI_API_KEY="",
    OPENAI_API_KEY="",
    ENVIRONMENT="development",
    RATE_LIMIT_PER_MINUTE="0",
    # Recovery is enabled, but no mail worker runs here: nothing is ever sent.
    SMTP_USER="browser-test@example.test",
    SMTP_PASSWORD="browser-test-only",
    SMTP_FROM_EMAIL="browser-test@example.test",
    OTP_SECRET="browser-test-only-otp-secret-0123456789abcdef",
    OTP_RESEND_SECONDS="30",
)

from app.core.config import Settings, get_settings  # noqa: E402

Settings.model_config["env_file"] = None
get_settings.cache_clear()
from app.main import create_app  # noqa: E402
from app.platform.models import Account, Base, Chat, ChatMessage, WakeelProfile  # noqa: E402
from app.platform.realtime import RealtimeHub  # noqa: E402
from app.platform import realtime  # noqa: E402
from app.platform.titles import TitleWorker  # noqa: E402
from app.platform.security import hash_password, throttle  # noqa: E402
from tests.test_platform import TestDatabase  # noqa: E402
from tests.test_api import services  # noqa: E402
from tests.test_qa_service import ScriptedModel, SECTION_3  # noqa: E402


# The responsive matrix deliberately performs dozens of full navigations per
# minute. Production ticket throttling is covered separately by backend tests.
async def browser_throttle(request, action, identity, limit, **kwargs):
    return await throttle(request, action, identity, 1000 if action == "socket_ticket" else limit, **kwargs)


realtime.throttle = browser_throttle
app = create_app()


class BrowserTitleGenerator:
    configured = True
    provider = "gemini"

    async def generate_json(self, system, user, schema, **kwargs):
        return {"title": "Gambling Law and Penalties"}


@asynccontextmanager
async def lifespan(app):
    with TemporaryDirectory(prefix="legalmind-browser-") as directory:
        db = TestDatabase("sqlite+aiosqlite:///" + str(Path(directory) / "test.db"))
        async with db.engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("platform_")]
                )
            )
        async with db.session() as s:
            for role in ("user", "wakeel", "admin"):
                account = Account(
                    email=role + "@example.test",
                    name=role.title() + " Tester",
                    role=role,
                    password_hash=hash_password("browser-test-password"),
                    city="Lahore",
                )
                s.add(account)
                await s.flush()
                if role == "user":
                    from uuid import uuid4

                    chat = Chat(
                        id="11111111-1111-4111-8111-111111111111",
                        user_id=account.id,
                        title="Long History Fixture",
                        title_status="ready",
                    )
                    s.add(chat)
                    await s.flush()
                    for index in range(55):
                        question = "Explain Section 3, part " + str(index)
                        s.add(
                            ChatMessage(
                                chat_id=chat.id,
                                request_id=str(uuid4()),
                                question=question,
                                response={
                                    "status": "partial",
                                    "question": question,
                                    "answer": "Browser-only conversation layout fixture. " * 18,
                                    "citations": [],
                                    "limitations": [],
                                },
                            )
                        )
                if role == "wakeel":
                    s.add(
                        WakeelProfile(
                            user_id=account.id,
                            bar_number="TEST-BAR",
                            specialization="Consumer law",
                            experience=5,
                            verification="verified",
                            biography="Browser test fixture",
                        )
                    )
            await s.commit()
        app.state.db = db
        app.state.realtime = RealtimeHub(db)
        await app.state.realtime.start()
        titles = TitleWorker(db, BrowserTitleGenerator(), app.state.realtime)
        titles.start()
        app.state.services = services(ScriptedModel(*([SECTION_3] * 100)))
        yield
        await titles.stop()
        await app.state.realtime.stop()
        await app.state.services.aclose()
        await db.engine.dispose()


app.router.lifespan_context = lifespan


# Browser tests read what a real inbox would have received. These routes exist
# only in this isolated test server, never in the application.
@app.get("/__test__/otp")
async def test_otp(email: str):
    from sqlalchemy import select

    from app.platform.mail import otp_value
    from app.platform.models import OtpChallenge

    async with app.state.db.session() as db:
        account = await db.scalar(select(Account).where(Account.email == email))
        challenge = await db.scalar(select(OtpChallenge).where(OtpChallenge.user_id == account.id)) if account else None
        return {"otp": otp_value(challenge.id, account.id) if challenge else None}


@app.get("/__test__/invitation")
async def test_invitation(email: str):
    from sqlalchemy import select

    from app.platform.models import MailOutbox

    async with app.state.db.session() as db:
        row = await db.scalar(
            select(MailOutbox).where(MailOutbox.email == email).order_by(MailOutbox.created_at.desc()).limit(1)
        )
        return {"token": row.payload.get("token") if row else None}
