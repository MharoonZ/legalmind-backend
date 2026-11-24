"""Shared runtime pieces: settings, rate limiting, inference queue, index refresh, logging, audit rows."""
import asyncio
import json
import logging
import threading

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.inference import InferenceCancelled, InferenceRuntime, ModelBusy
from app.core.logging import _JsonFormatter, _RequestContext, configure_logging, request_id
from app.core.ratelimit import RateLimiter
from app.qa.records import QaRecordRepository
from app.retrieval import index as index_module
from app.retrieval.index import PublicationIndex
from app.retrieval.snapshot import SnapshotError
from tests.corpus import make_snapshot


def make_settings(**overrides):
    return Settings(database_url="postgresql://u:p@localhost/db", _env_file=None, **overrides)


# ------------------------------------------------------------------ settings
def test_settings_normalise_and_validate_the_environment():
    assert make_settings(environment=" Production ").is_production
    assert make_settings().is_development and make_settings().serve_docs
    assert not make_settings(environment="production").serve_docs
    assert make_settings(environment="production", docs_enabled=True).serve_docs
    with pytest.raises(ValidationError):
        make_settings(environment="prod-ish")
    with pytest.raises(ValidationError):
        make_settings(log_level="loud")
    for bad in ({"upload_max_pages": 0}, {"legal_upload_max_size_bytes": -1}, {"qa_rerank_mode": "sometimes"},
                {"gemini_model": "bad model name"}, {"qa_max_concurrent_requests": 0}):
        with pytest.raises(ValidationError):
            make_settings(**bad)


def test_answer_model_selection_and_secrets_stay_hidden():
    none = make_settings()
    assert not none.use_gemini and not none.llm_configured and none.active_model == "gpt-4o-mini"
    gemini = make_settings(gemini_api_key="secret-value")
    assert gemini.use_gemini and gemini.llm_configured and gemini.active_model == "gemini-3.5-flash-lite"
    assert "secret-value" not in repr(gemini) and "secret-value" not in gemini.model_dump_json()
    forced = make_settings(llm_provider="openai", gemini_api_key="g", openai_api_key="o")
    assert not forced.use_gemini and forced.llm_configured
    assert make_settings(api_keys=" a , b ,, ").api_key_set == {"a", "b"}
    assert make_settings(gemini_fallback_model="  ").gemini_fallback_model is None


def test_old_environment_files_keep_working():
    legacy = Settings(database_url="postgres://u:p@h/db", _env_file=None, EMBEDDING_MODEL="x",
                      RETRIEVAL_TOP_K="5", LEGAL_SEARCH_HNSW_EF_SEARCH="200")
    assert legacy.database_url.startswith("postgresql+asyncpg://")


# -------------------------------------------------------------- rate limiter
def test_rate_limiter_is_a_sliding_window_per_client():
    limiter = RateLimiter(window_seconds=60)
    assert [limiter.allow("a", 2, now=t)[0] for t in (0, 1, 2)] == [True, True, False]
    assert limiter.allow("b", 2, now=2) == (True, 0.0)
    allowed, retry_after = limiter.allow("a", 2, now=30)
    assert not allowed and retry_after == pytest.approx(30)
    assert limiter.allow("a", 2, now=61)[0]
    assert limiter.allow("anyone", 0, now=0) == (True, 0.0)


# --------------------------------------------------------- inference runtime
def test_inference_runs_one_job_at_a_time_and_queues_the_rest():
    runtime = InferenceRuntime(max_pending=8)
    active, peak, order = [], [], []

    def job(tag):
        def work(stop):
            active.append(tag)
            peak.append(len(active))
            order.append(tag)
            active.remove(tag)
            return tag
        return work

    async def main():
        return await asyncio.gather(*(runtime.run(job(index), stage="test") for index in range(6)))
    assert asyncio.run(main()) == list(range(6)) and max(peak) == 1 and runtime.pending == 0
    runtime.shutdown()


def test_a_full_queue_is_refused_and_cancelled_work_is_skipped():
    runtime = InferenceRuntime(max_pending=1)
    release, started = threading.Event(), threading.Event()
    ran = []

    def blocking(stop):
        started.set()
        release.wait(5)
        return "done"

    async def main():
        first = asyncio.create_task(runtime.run(blocking, stage="test"))
        await asyncio.to_thread(started.wait, 5)
        with pytest.raises(ModelBusy):
            await runtime.run(lambda stop: ran.append("overflow"), stage="test")
        release.set()
        return await first
    assert asyncio.run(main()) == "done" and ran == [] and runtime.pending == 0

    waiting = InferenceRuntime(max_pending=4)
    release.clear()
    started.clear()

    async def cancelled():
        first = asyncio.create_task(waiting.run(blocking, stage="test"))
        await asyncio.to_thread(started.wait, 5)
        second = asyncio.create_task(waiting.run(lambda stop: ran.append("late"), stage="test"))
        await asyncio.sleep(0.05)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        await first
    asyncio.run(cancelled())
    assert ran == [] and waiting.pending == 0
    assert issubclass(InferenceCancelled, RuntimeError)


# -------------------------------------------------------------- index refresh
def test_index_loads_once_and_reloads_only_when_the_publication_changes(monkeypatch):
    snapshot = make_snapshot()
    state = {"identity": (snapshot.id, snapshot.manifest_sha256), "loads": 0, "result": snapshot}

    async def identity(db):
        return state["identity"]

    async def load(db, *, cache_dir=None):
        state["loads"] += 1
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]
    monkeypatch.setattr(index_module, "active_identity", identity)
    monkeypatch.setattr(index_module, "load_snapshot", load)
    index = PublicationIndex(db=object(), refresh_seconds=60)

    async def scenario():
        assert await index.ensure_loaded() is snapshot and state["loads"] == 1
        assert await index.refresh() is False and state["loads"] == 1        # unchanged: one small query
        state["identity"] = (snapshot.id, "b" * 64)                          # another publication activated
        assert await index.refresh() is False and state["loads"] == 2        # same content came back
        state["result"] = ConnectionError("database down")
        state["identity"] = (snapshot.id, "c" * 64)
        assert await index.refresh() is False and index.snapshot is snapshot  # keep serving what we have
        assert index.status()["last_error"] == "ConnectionError"
        state["result"] = SnapshotError("chunk count changed")
        assert await index.refresh() is False and index.snapshot is None      # inconsistent: fail closed
        state["result"] = None
        await index.refresh()
        assert index.snapshot is None and index.status()["loaded"] is False
    asyncio.run(scenario())


def test_index_background_refresh_starts_and_stops(monkeypatch):
    calls = []

    async def load(db, *, cache_dir=None):
        calls.append(1)
        return None
    monkeypatch.setattr(index_module, "load_snapshot", load)
    index = PublicationIndex(db=object(), refresh_seconds=0.01)

    async def scenario():
        index.start()
        await asyncio.sleep(0.08)
        await index.stop()
        count = len(calls)
        await asyncio.sleep(0.05)
        assert count >= 2 and len(calls) == count
    asyncio.run(scenario())
    fixed = PublicationIndex.from_snapshot(None)
    assert asyncio.run(fixed.refresh()) is False


# -------------------------------------------------------------------- logging
def test_log_lines_carry_the_request_id_and_can_be_json():
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "stage=%s", ("retrieval",), None)
    token = request_id.set("req-42")
    try:
        assert _RequestContext().filter(record) and record.request_id == "req-42"
    finally:
        request_id.reset(token)
    payload = json.loads(_JsonFormatter().format(record))
    assert payload["message"] == "stage=retrieval" and payload["request_id"] == "req-42"
    assert payload["level"] == "INFO" and payload["logger"] == "app.test"
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    try:
        configure_logging("warning", "json")
        assert root.level == logging.WARNING and isinstance(root.handlers[0].formatter, _JsonFormatter)
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)


# ---------------------------------------------------------------- audit rows
def test_audit_recording_writes_the_run_its_hits_and_cited_evidence():
    from tests.test_qa_service import SECTION_3, ScriptedModel, ask, build
    from app.schemas.qa import AskIn

    class Session:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Db:
        def __init__(self):
            self.sessions = []

        def session(self):
            self.sessions.append(Session())
            return self.sessions[-1]

    db = Db()
    snapshot = make_snapshot()
    service = build(snapshot, ScriptedModel(SECTION_3), recorder=QaRecordRepository(db))
    result, _ = ask(service, "What is Section 3 of the gambling ordinance?")
    rows = db.sessions[0].rows
    kinds = [type(row).__name__ for row in rows]
    assert kinds == ["QaRun", "RetrievalHit", "AnswerClaim", "AnswerEvidence"]
    run, hit, claim, evidence = rows
    assert run.outcome == "answered" and run.model == "scripted-model" and run.reranker_model is None
    assert hit.exact_rank == 1 and hit.chunk_id == result.citations[0].chunk_id
    assert claim.evidence_id == result.citations[0].evidence_id and evidence.page_index == result.citations[0].page_index
    assert AskIn(question="x1").history == []
