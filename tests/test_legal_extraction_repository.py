"""Exercise real ORM persistence in a disposable SQLite transaction adapter.

PostgreSQL-specific checks/FKs and row locks are NOT simulated by SQLite.
The optional PostgreSQL suite separately verifies concurrent claims.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import JSON, DefaultClause, MetaData, UniqueConstraint, create_engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db.legal_corpus_models import FileBlock, FileExtraction, FilePage, JobAttempt, LegalFile, ProcessingJob
from app.repositories.legacy_legal_extraction_repository import JobConflict, LeaseLost, LegalExtractionRepository, claim_statement
from app.services.legal_pdf_extraction import extraction_config, failed_page, page_result
from tests.test_legal_pdf_extraction import native_pdf

MODELS = (LegalFile, FileExtraction, FilePage, FileBlock, ProcessingJob, JobAttempt)


class SQLiteRepository(LegalExtractionRepository):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    async def _now(self, session): return self.now


class AsyncAdapter:
    """Only adapts IO; all SQL queries, writes, transactions and results are real."""
    def __init__(self, session): self.session = session
    @asynccontextmanager
    async def begin(self):
        with self.session.begin(): yield
    async def execute(self, *args): return self.session.execute(*args)
    async def scalars(self, *args): return self.session.scalars(*args)
    async def get(self, *args): return self.session.get(*args)
    async def flush(self): self.session.flush()
    def add(self, value): self.session.add(value)


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'worker-test.sqlite').as_posix()}")
    metadata = MetaData()
    for model in MODELS:
        table = model.__table__.to_metadata(metadata)
        # SQLite cannot express PG JSONB casts, regex checks or unrelated FKs.
        for constraint in list(table.constraints):
            if not isinstance(constraint, UniqueConstraint) and constraint is not table.primary_key:
                table.constraints.remove(constraint)
        table.foreign_keys.clear()
        for column in table.c:
            column.foreign_keys.clear()
            if isinstance(column.type, JSONB):
                column.type = JSON()
                if column.server_default is not None:
                    column.server_default = DefaultClause(text("'[]'"))
    metadata.create_all(engine)
    @asynccontextmanager
    async def sessions():
        with Session(engine, expire_on_commit=False) as session:
            yield AsyncAdapter(session)
    repo = SQLiteRepository(sessions, lease_seconds=30, max_attempts=2)
    repo.engine = engine
    yield repo
    engine.dispose()


def seed(repository, *, pages=2):
    file_id, job_id = uuid4(), uuid4()
    digest = file_id.hex * 2
    with Session(repository.engine) as session, session.begin():
        session.add(LegalFile(id=file_id, original_filename="synthetic.pdf", object_key=f"{digest[:2]}/{digest}.pdf",
                             sha256=digest, byte_size=100, media_type="application/pdf", page_count=pages))
        session.flush()
        session.add(ProcessingJob(id=job_id, task_type="extract", target_file_id=file_id,
                                  idempotency_key=f"extract:{file_id}", status="queued", progress_total=pages, progress_done=0))
    return job_id


def config(settings):
    return extraction_config(settings.model_copy(update={"legal_ocr_enabled": False}))


def test_claim_sql_uses_skip_locked_and_filters_extract_jobs():
    job_id = uuid4()
    sql = str(claim_statement(datetime.now(UTC), job_id).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE SKIP LOCKED" in sql and "LIMIT 1" in sql
    assert str(job_id) in sql and "'extract'" in sql and "target_file_id IS NOT NULL" in sql


@pytest.mark.anyio
async def test_claim_progress_idempotency_and_exact_coverage(repository, settings):
    job_id = seed(repository)
    other = seed(repository)
    claim = await repository.claim("worker-a", job_id)
    assert claim.job_id == job_id
    assert await repository.claim("worker-b", job_id) is None
    assert (await repository.inspect_job(other))["status"] == "queued"
    extraction_id, _ = await repository.prepare(claim, config(settings))
    await repository.set_total(claim, 2, extraction_id)
    with native_pdf() as doc:
        result = page_result(doc[0], config(settings))
    await repository.save_page(claim, extraction_id, result)
    await repository.save_page(claim, extraction_id, result)
    assert (await repository.inspect_job(job_id))["progress_done"] == 1
    with Session(repository.engine) as session:
        assert len(session.scalars(select(FilePage)).all()) == 1
        assert len(session.scalars(select(FileBlock)).all()) == len(result["blocks"])
    with pytest.raises(JobConflict, match="incomplete_page_coverage"):
        await repository.finish(claim, extraction_id, 2)
    await repository.save_page(claim, extraction_id, failed_page(2, "ocr_unavailable"))
    report = await repository.finish(claim, extraction_id, 2)
    assert report["failed"] == 1 and report["human_verified"] is False
    assert len(report["output_sha256"]) == 64
    with pytest.raises(LeaseLost):
        await repository.save_page(claim, extraction_id, result)


@pytest.mark.anyio
async def test_expired_worker_is_fenced_and_pages_resume(repository, settings):
    job_id = seed(repository)
    old = await repository.claim("old", job_id)
    extraction_id, _ = await repository.prepare(old, config(settings))
    await repository.set_total(old, 2, extraction_id)
    await repository.save_page(old, extraction_id, failed_page(1, "ocr_missing"))
    repository.now += timedelta(seconds=31)
    current = await repository.claim("new", job_id)
    assert current.attempt_id != old.attempt_id
    assert (await repository.prepare(current, config(settings)))[0] == extraction_id
    await repository.set_total(current, 2, extraction_id)
    assert (await repository.inspect_job(job_id))["progress_done"] == 1
    for operation in (repository.heartbeat(old), repository.save_page(old, extraction_id, failed_page(2, "late")),
                      repository.fail(old, extraction_id, "late")):
        with pytest.raises(LeaseLost): await operation
    await repository.save_page(current, extraction_id, failed_page(2, "ocr_missing"))
    await repository.finish(current, extraction_id, 2)
    attempts = (await repository.inspect_job(job_id))["attempts"]
    assert [a["status"] for a in attempts] == ["failed", "completed"]
    assert attempts[0]["error"] == "lease_expired"


@pytest.mark.anyio
async def test_bounded_attempts_and_explicit_retry_preserve_old_rows(repository, settings):
    job_id = seed(repository, pages=1)
    old_id = None
    for _ in range(2):
        claim = await repository.claim("worker", job_id)
        old_id, _ = await repository.prepare(claim, config(settings))
        await repository.set_total(claim, 1, old_id)
        await repository.save_page(claim, old_id, failed_page(1, "pdf_unreadable"))
        await repository.fail(claim, old_id, "pdf_unreadable")
        repository.now += timedelta(seconds=1)
    assert (await repository.inspect_job(job_id))["status"] == "failed"
    assert await repository.claim("worker", job_id) is None
    await repository.retry(job_id)
    retry = await repository.claim("worker", job_id)
    new_id, _ = await repository.prepare(retry, config(settings))
    assert new_id != old_id
    assert await repository.coverage(old_id) == {1}
    assert await repository.coverage(new_id) == set()


@pytest.mark.anyio
async def test_expired_attempt_limit_stops_reclaiming(repository, settings):
    job_id = seed(repository)
    for _ in range(2):
        assert await repository.claim("crashed", job_id)
        repository.now += timedelta(seconds=31)
    assert await repository.claim("third", job_id) is None
    assert (await repository.inspect_job(job_id))["error"] == "attempt_limit_reached"


@pytest.mark.anyio
async def test_invalid_offsets_and_hashes_rollback(repository, settings):
    claim = await repository.claim("worker", seed(repository))
    extraction_id, _ = await repository.prepare(claim, config(settings))
    await repository.set_total(claim, 2, extraction_id)
    with native_pdf() as doc: result = page_result(doc[0], config(settings))
    result["blocks"][0]["page_char_end"] = 9999
    with pytest.raises(JobConflict, match="invalid_block_offsets"):
        await repository.save_page(claim, extraction_id, result)
    assert await repository.coverage(extraction_id) == set()
    bad = failed_page(1, "test")
    bad["text_sha256"] = "a" * 64
    with pytest.raises(JobConflict, match="page_text_hash_mismatch"):
        await repository.save_page(claim, extraction_id, bad)
