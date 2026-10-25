"""Optional real PostgreSQL concurrency test, on an explicit LOCAL test DB.

Never falls back to DATABASE_URL or the project's .env. Creates and removes
one random test schema. Set LEGAL_EXTRACTION_TEST_DATABASE_URL to a loopback
database whose name starts with legalmind_test to opt in.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import legal_corpus_models as models
from app.repositories.legacy_legal_extraction_repository import LegalExtractionRepository


@pytest.mark.anyio
async def test_two_postgres_workers_cannot_claim_same_job():
    value = os.environ.get("LEGAL_EXTRACTION_TEST_DATABASE_URL")
    if not value:
        pytest.skip("No disposable local PostgreSQL test database configured; SQLite does not prove row locking.")
    url = make_url(value)
    assert url.host in {"localhost", "127.0.0.1", "::1"} and (url.database or "").startswith("legalmind_test")
    schema = "extract_test_" + uuid4().hex
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema}, "timeout": 5})
    metadata = MetaData(schema=schema)
    for model in vars(models).values():
        if isinstance(model, type) and getattr(model, "__module__", "") == models.__name__ and hasattr(model, "__table__"):
            model.__table__.to_metadata(metadata)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with session_factory() as session: yield session
    created = False
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
            await conn.run_sync(metadata.create_all)
        file_id, job_id = uuid4(), uuid4()
        async with session_factory() as session, session.begin():
            session.add(models.LegalFile(id=file_id, original_filename="test.pdf", object_key="test.pdf",
                sha256="a" * 64, byte_size=100, media_type="application/pdf", page_count=1))
            await session.flush()
            session.add(models.ProcessingJob(id=job_id, task_type="extract", target_file_id=file_id,
                idempotency_key=f"extract:{file_id}", status="queued", progress_total=1, progress_done=0))
        repository = LegalExtractionRepository(sessions)
        claims = await asyncio.gather(repository.claim("first", job_id), repository.claim("second", job_id))
        assert sum(claim is not None for claim in claims) == 1
        assert len((await repository.inspect_job(job_id))["attempts"]) == 1
    finally:
        if created:
            async with engine.begin() as conn: await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
