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
)

from app.core.config import Settings, get_settings  # noqa: E402

Settings.model_config["env_file"] = None
get_settings.cache_clear()
from app.main import create_app  # noqa: E402
from app.platform.models import Account, Base, WakeelProfile  # noqa: E402
from app.platform.security import hash_password  # noqa: E402
from tests.test_platform import TestDatabase  # noqa: E402
from tests.test_api import services  # noqa: E402
from tests.test_qa_service import ScriptedModel, SECTION_3  # noqa: E402

app = create_app()


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
        app.state.services = services(ScriptedModel(*([SECTION_3] * 100)))
        yield
        await app.state.services.aclose()
        await db.engine.dispose()


app.router.lifespan_context = lifespan
