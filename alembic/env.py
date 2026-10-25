"""Run schema migrations with the same async driver as the application."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import prepared_statement_name
from app.db import legal_corpus_models  # noqa: F401 - register Haroon's tables
from app.db import models  # noqa: F401 - register existing tables
from app.db import publication_models  # noqa: F401 - register publication tables
from app.db import qa_record_models  # noqa: F401 - register Phase C tables
from app.embeddings import models as embedding_models  # noqa: F401 - register embedding tables

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""
    settings = get_settings()
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = get_settings()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url
    connect_args = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": prepared_statement_name,
        "timeout": settings.db_connect_timeout,
        "command_timeout": settings.db_command_timeout,
    }
    if settings.db_ssl:
        connect_args["ssl"] = "require"
    engine = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
