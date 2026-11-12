"""Database layer.

One `Database` object is created when the app starts and shared by every
request. It owns a single connection pool, so we connect to Postgres one
time and reuse those connections for the whole life of the web app.

Why this stays stable:
- `pool_pre_ping`  : a dead connection is detected and replaced before use.
- `pool_recycle`   : old connections are replaced before Supabase closes them.
- pool size limits : we never open more connections than the Supabase pooler allows.
- startup retries  : a short network problem at boot does not kill the app.
"""

import asyncio
import logging
from uuid import uuid4
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

logger = logging.getLogger(__name__)


def prepared_statement_name() -> str:
    """Never reuse the unnamed statement slot across pooler/type introspection calls."""
    return f"__legalmind_{uuid4().hex}__"


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        connect_args: dict = {
            "timeout": settings.db_connect_timeout,
            "command_timeout": settings.db_command_timeout,
            # Needed for Supabase pooler (pgbouncer style). Safe on a direct connection too.
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": prepared_statement_name,
        }
        if settings.db_ssl:
            connect_args["ssl"] = "require"

        # Creating the engine does not open a connection yet; that happens in connect().
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args=connect_args,
        )
        self._session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def host(self) -> str | None:
        """Safe to log (never includes the password)."""
        return self.engine.url.host

    async def ping(self, timeout: float | None = None) -> bool:
        """Cheap check that the database answers. Never raises.

        Defaults to DB_CONNECT_TIMEOUT: a far-away database (for example a
        Sydney Supabase project reached from South Asia) needs seconds, not
        milliseconds, to finish the TLS handshake and login.
        """
        limit = timeout if timeout is not None else self._settings.db_connect_timeout
        try:
            async with asyncio.timeout(limit):
                async with self.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - health check must not crash the app
            logger.warning("database_ping_failed error=%s timeout_seconds=%g", type(exc).__name__, limit)
            return False

    async def connect(self) -> bool:
        """Open the first connection at startup, with a few retries.

        Returns True if the database is reachable. If it is not, the app still
        starts (the pool will keep trying on later requests) and /health shows "degraded".
        """
        attempts = max(1, self._settings.db_connect_retries)
        for attempt in range(1, attempts + 1):
            if await self.ping(timeout=self._settings.db_connect_timeout):
                logger.info("Database connected (host=%s)", self.host)
                return True
            logger.warning("Database connection attempt %d/%d failed", attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(self._settings.db_connect_retry_delay)

        logger.error("Database unreachable after %d attempts; running without it", attempts)
        return False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A short-lived session that borrows a connection from the shared pool."""
        async with self._session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Close all pooled connections (called once on shutdown)."""
        await self.engine.dispose()
        logger.info("Database pool closed")
