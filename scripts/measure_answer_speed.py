r"""Measure the real answer path without OpenAI calls or database writes.

Run from this backend: .\.venv\Scripts\python.exe -u -m scripts.measure_answer_speed
Only stage names, timings and counts are printed/saved under ignored tmp/.
The source-proof check uses a real stored passage, not an LLM-generated answer.
"""
from __future__ import annotations

import asyncio
import argparse
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from time import perf_counter

os.environ["OPENAI_API_KEY"] = ""
os.environ["QA_RECORD_RUNS"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
logging.disable(logging.CRITICAL)

from sqlalchemy import event, select, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.qa_config import get_reranker_settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.db.publication_models import CorpusPublication  # noqa: E402
from app.repositories.legal_search_repository import LegalSearchRepository  # noqa: E402
from app.repositories.qa_context_repository import QaContextRepository  # noqa: E402
from app.schemas.legal_search import LegalSearchIn  # noqa: E402
from app.services.legal_retrieval import LegalRetrievalService  # noqa: E402
from app.services.qa.context import ContextExpander  # noqa: E402
from app.schemas.qa_decision import PreparedQuestion
from app.services.qa.context_validation import validate_context  # noqa: E402
from app.services.qa.reranker import CandidateReranker, FlagEmbeddingScorer  # noqa: E402
from app.services.qa.source_verification import QaSourceVerifier  # noqa: E402


class Timings:
    def __init__(self):
        self.stage = ContextVar("answer_speed_stage", default="startup")
        self.rows = []
        self.sql = defaultdict(lambda: {"queries": 0, "seconds": 0.0})

    @contextmanager
    def measure(self, name):
        print(f"START {name}", flush=True)
        token = self.stage.set(name)
        started = perf_counter()
        success = False
        try:
            yield
            success = True
        finally:
            seconds = perf_counter() - started
            row = {"stage": name, "seconds": round(seconds, 3), "success": success,
                   "database_queries": self.sql[name]["queries"],
                   "database_query_seconds": round(self.sql[name]["seconds"], 3)}
            self.rows.append(row)
            self.stage.reset(token)
            print(json.dumps(row), flush=True)

    async def run(self, name, awaitable):
        with self.measure(name):
            return await awaitable

    def attach(self, db):
        @event.listens_for(db.engine.sync_engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, executemany):
            operation = statement.lstrip().split(None, 1)[0].upper()
            if operation in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP",
                             "TRUNCATE", "COPY", "GRANT", "REVOKE"}:
                raise RuntimeError("The read-only profiler blocked a database write.")
            context._speed_start = (self.stage.get(), perf_counter())

        @event.listens_for(db.engine.sync_engine, "after_cursor_execute")
        def after(conn, cursor, statement, parameters, context, executemany):
            name, started = context._speed_start
            self.sql[name]["queries"] += 1
            self.sql[name]["seconds"] += perf_counter() - started


class MeasuredScorer(FlagEmbeddingScorer):
    def __init__(self, settings, timings):
        super().__init__(settings)
        self.timings = timings

    def _load(self):
        with self.timings.measure("reranker.model_load"):
            return super()._load()


class MeasuredContextRepository(QaContextRepository):
    counts = None

    @asynccontextmanager
    async def snapshot(self, publication_id, *, instrument_ids=None):
        async with super().snapshot(publication_id, instrument_ids=instrument_ids) as catalog:
            yield catalog
            self.counts = {"published_instruments": len(catalog.items),
                           "loaded_instruments": len(catalog._loaded_instruments),
                           "chunks": len(catalog.chunks), "provisions": len(catalog.nodes)}


def safe_error_details(exc):
    """Keep connection parameters and SQL values out of diagnostic output."""
    pending, seen, types, states, messages = [exc], set(), set(), set(), []
    while pending:
        value = pending.pop()
        if value is None or id(value) in seen:
            continue
        seen.add(id(value))
        types.add(type(value).__name__)
        state = getattr(value, "sqlstate", None) or getattr(value, "pgcode", None)
        if isinstance(state, str) and len(state) == 5 and state.isalnum():
            states.add(state)
        messages.append(str(value).lower())  # Inspect privately; never output these strings.
        pending.extend([getattr(value, "orig", None), value.__cause__, value.__context__])
    markers = {
        "statement_timeout": "statement timeout",
        "connection_closed": "connection was closed",
        "connection_reset": "connection reset",
        "prepared_statement_error": "prepared statement",
        "pool_wait_timeout": "queuepool limit",
    }
    return {"types": sorted(types), "sqlstates": sorted(states),
            "categories": [name for name, phrase in markers.items()
                           if any(phrase in message for message in messages)]}


async def database_probe(db, timings, report):
    async with db.session() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        for index in range(1, 4):
            await timings.run(f"database.select_one.{index}", session.scalar(text("SELECT 1")))
        plan = await timings.run("database.explain_select_one", session.scalar(text(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT 1")))
        if isinstance(plan, str):
            plan = json.loads(plan)
        report["select_one_server_execution_ms"] = plan[0].get("Execution Time")
        report["select_one_server_planning_ms"] = plan[0].get("Planning Time")
        publication_id = await timings.run("database.active_publication_id", session.scalar(
            select(CorpusPublication.id).where(CorpusPublication.status == "active")))
    if publication_id is not None:
        with timings.measure("context_catalog_only"):
            async with QaContextRepository(db).snapshot(publication_id) as catalog:
                report["catalog_counts"] = {"instruments": len(catalog.items),
                                            "chunks": len(catalog.chunks), "provisions": len(catalog.nodes)}


async def main(database_only=False):
    started = perf_counter()
    timings = Timings()
    report = {"openai_requests": 0, "audit_writes": False, "stages": timings.rows,
              "scope": ("simple database round trips and real context catalog; no model loading"
                        if database_only else
                        "one exact-reference request; real read-only corpus, cached models")}
    db = Database(get_settings())
    timings.attach(db)
    failed = False
    try:
        if not await timings.run("database.initial_connection", db.ping(timeout=30)):
            raise RuntimeError("Database did not respond to the read-only check.")
        if database_only:
            await database_probe(db, timings, report)
            return 0
        retrieval = LegalRetrievalService(LegalSearchRepository(db), get_settings())
        result = await timings.run("retrieval", retrieval.search(LegalSearchIn(
            query="What does Section 19 of the Consumer Protection Act say?", limit=10)))
        report["retrieval_status"] = result.status
        report["candidates"] = len(result.results)
        if result.status != "ok" or not result.results:
            raise RuntimeError("The profiler question has no usable published evidence.")
        settings = get_reranker_settings()
        reranker = CandidateReranker(settings, scorer=MeasuredScorer(settings, timings))
        await timings.run("reranker.startup_load", asyncio.to_thread(reranker.scorer.load))
        reranked = await timings.run("reranker.first_request_after_warmup", reranker.rerank(result))
        # Same frozen candidates and model instance; no further retrieval or AI request.
        await timings.run("reranker.warm_request", reranker.rerank(result))
        context_repository = MeasuredContextRepository(db)
        context = await timings.run("context_expansion", ContextExpander(context_repository).expand(reranked))
        report["context_catalog_counts"] = context_repository.counts
        with timings.measure("source_preparation"):
            validate_context(context)
            decision = PreparedQuestion(outcome="ready_for_prompt", reason="Prepared context", context=context, evidence_ids=tuple(u.evidence_id for u in context.units))
        report["preparation_outcome"] = decision.outcome
        report["context_units"] = len(context.units)
        report["context_critical_issues"] = sum(issue.critical for issue in context.issues)
        proof_ranges = list((
            (unit, ref["chunk_char_start"], min(ref["chunk_char_end"], ref["chunk_char_start"] + 180))
            for unit in context.units if unit.source_kind == "chunk"
            for ref in unit.source_refs
            if isinstance(ref.get("chunk_char_start"), int) and
               isinstance(ref.get("chunk_char_end"), int) and
               ref["chunk_char_end"] - ref["chunk_char_start"] >= 20
        ))[:5]
        if not proof_ranges:
            raise RuntimeError("No stored source fragment is available to measure verification.")
        verifier = QaSourceVerifier(db)
        proofs = await timings.run("source_verification.claim_batch", verifier.verify_many(proof_ranges))
        report["source_proof_passed"] = all(proofs)
        report["source_claim_ranges"] = len(proof_ranges)
        report["source_proof_count"] = sum(map(len, proofs))
    except Exception as exc:
        # SQL/SDK exception messages can contain private request/connection details.
        report["error_type"] = type(exc).__name__
        report["error_details"] = safe_error_details(exc)
        failed = True
    finally:
        await db.dispose()
        report["total_seconds"] = round(perf_counter() - started, 3)
        report["llm_latency"] = "not measured: no OpenAI request was made"
        root = Path(__file__).resolve().parents[1]
        output = root / "tmp" / ("answer-speed-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output.relative_to(root)), "summary": report}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-only", action="store_true",
                        help="Measure simple database round trips and the context catalog; do not load models.")
    raise SystemExit(asyncio.run(main(database_only=parser.parse_args().database_only)))
