"""Worker run loop: polling, transient failures and releasing a job on shutdown."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.legal_extraction import ExtractionConfig
from app.workers import legal_extract, legal_structure
from app.workers.runner import run_jobs


def test_single_run_processes_one_job_and_propagates_errors():
    calls = []

    async def once():
        calls.append(1)
        return True
    asyncio.run(run_jobs(once, watch=False, poll_seconds=1))
    assert calls == [1]

    async def failing():
        raise ConnectionError("database restarted")
    with pytest.raises(ConnectionError):
        asyncio.run(run_jobs(failing, watch=False, poll_seconds=1))


def test_watch_mode_survives_transient_failures_with_backoff():
    outcomes = iter([True, ConnectionError("blip"), ConnectionError("blip"), False, True])
    sleeps = []

    async def step():
        outcome = next(outcomes, None)
        if outcome is None:
            raise asyncio.CancelledError()          # stop the loop for the test
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(seconds):
        sleeps.append(seconds)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_jobs(step, watch=True, poll_seconds=2, sleep=sleep))
    # Two failures back off (4 s, 8 s); an empty queue waits one poll interval.
    assert sleeps == [4, 8, 2]


class FakeExtractionRepo:
    def __init__(self):
        self.failed, self.lease = None, None

    async def claim_next(self, *, worker_id, config, expected_sha256=None, lease_seconds=3600):
        self.lease = lease_seconds
        return SimpleNamespace(job_id=uuid4(), file_id=uuid4(), extraction_id=uuid4(), attempt_id=uuid4(),
                               object_key="aa/missing.pdf", sha256="a" * 64, page_count=1)

    async def fail(self, claim, message):
        self.failed = message


def test_a_stopped_extraction_worker_releases_its_job(monkeypatch, tmp_path):
    repo = FakeExtractionRepo()

    def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(legal_extract, "resolve_source_pdf", interrupted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(legal_extract.process_one(repo, upload_dir=str(tmp_path),
                                              config=ExtractionConfig(tessdata_dir="unused"), lease_seconds=120))
    assert repo.lease == 120 and "retry the job" in repo.failed


def test_an_extraction_error_is_recorded_and_the_worker_continues(tmp_path):
    repo = FakeExtractionRepo()
    worked = asyncio.run(legal_extract.process_one(repo, upload_dir=str(tmp_path),
                                                   config=ExtractionConfig(tessdata_dir="unused")))
    assert worked is True and repo.failed          # the missing PDF fails this job only


def test_a_stopped_structure_worker_releases_its_job():
    class Repo:
        failed = None
        lease = None

        async def claim_next(self, *, worker_id, lease_seconds=3600):
            Repo.lease = lease_seconds
            return SimpleNamespace(job_id=uuid4(), revision_id=uuid4(), attempt_id=uuid4())

        async def load_sources(self, revision_id):
            raise asyncio.CancelledError()

        async def fail(self, claim, message):
            Repo.failed = message
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(legal_structure.process_one(Repo(), lease_seconds=300))
    assert Repo.lease == 300 and "retry the job" in Repo.failed

    class Empty(Repo):
        async def claim_next(self, *, worker_id, lease_seconds=3600):
            return None
    assert asyncio.run(legal_structure.process_one(Empty())) is False
