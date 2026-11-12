"""Compare retrieval and the answer gate through the actual ASGI routes."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter

import httpx

from app.services.qa_generation import GenerationUnavailable


def relevance(candidates, expected, k):
    """Credit each expected provision once, even when several chunks match."""
    hit, gains = set(), []
    for row in candidates[:k]:
        new = {index for index, (law, label) in enumerate(expected)
               if law.casefold() in row["instrument_title"].casefold()
               and label == row["label"]} - hit
        hit.update(new)
        gains.append(int(bool(new)))
    return gains


def ranking_metrics(candidates, expected):
    if not expected:
        return {}
    gains = relevance(candidates, expected, 10)
    metrics = {"mrr_at_10": next((1 / rank for rank, gain in enumerate(gains, 1) if gain), 0)}
    for k in (5, 10):
        dcg = sum(gain / math.log2(i + 2) for i, gain in enumerate(gains[:k]))
        ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(expected))))
        metrics.update({
            f"recall_at_{k}": sum(gains[:k]) / len(expected),
            f"precision_at_{k}": sum(gains[:k]) / k,
            f"ndcg_at_{k}": dcg / ideal,
        })
    return metrics


def summary(results):
    metrics = {}
    for name in ("retrieval", "reranked"):
        rankings = [ranking_metrics(row[name], row["expected"]) for row in results if name in row]
        keys = {key for row in rankings for key in row}
        metrics[name] = {key: statistics.mean(row[key] for row in rankings if key in row)
                         for key in sorted(keys)}
    for name in ("search_seconds", "ask_seconds"):
        values = sorted(row[name] for row in results if name in row)
        if values:
            metrics[name] = {"p50": statistics.median(values),
                             "p95": values[math.ceil(.95 * len(values)) - 1]}
    for expected_ready, key in ((True, "false_rejection"), (False, "false_acceptance")):
        rows = [row for row in results if row.get("expected_gate")
                and (row["expected_gate"] == "ready_for_prompt") == expected_ready]
        if rows:
            metrics[key] = {"count": sum((row["gate"] == "ready_for_prompt") != expected_ready
                                         for row in rows), "total": len(rows)}
    absent = [row for row in results if row.get("expected_gate") == "insufficient_evidence"]
    if absent:
        metrics["no_evidence_detection"] = {"correct": sum(row["gate"] == "insufficient_evidence"
                                                           for row in absent), "total": len(absent)}
    exact = [row for row in results if row["category"] == "exact" and row["expected"]]
    if exact:
        metrics["exact_top_1"] = sum(bool(sum(relevance(row["retrieval"], row["expected"], 1)))
                                     for row in exact) / len(exact)
    return metrics


def candidate_rows(rows):
    keys = ("chunk_id", "root_provision_id", "instrument_title", "label", "reference_path",
            "matched_reference_paths", "rank", "exact_rank", "keyword_rank", "vector_rank",
            "rrf_rank", "vector_score")
    return [{key: row.get(key) for key in keys} for row in rows]


class GateOnly:
    async def generate(self, *args, **kwargs):
        raise GenerationUnavailable("Generation disabled in gate-only benchmark.")


async def run(args):
    os.environ.update(LEGAL_SEARCH_ENABLED="true", LEGAL_INGEST_ENABLED="false",
                      QA_RECORD_RUNS="false", LLM_PROVIDER="gemini")
    # The benchmark has explicit control of when prompts are sent externally.
    if not args.gemini:
        os.environ["GEMINI_API_KEY"] = ""
        os.environ["OPENAI_API_KEY"] = ""
    # Avoid logging question text, SQL parameters and provider request details.
    logging.disable(logging.CRITICAL)

    from app.api.routes.qa import get_question_answer_service
    from app.core.config import get_settings
    from app.core.qa_generation_config import get_answer_settings
    from app.main import create_app

    if args.gemini and not (get_answer_settings().gemini_api_key and
                           get_answer_settings().gemini_api_key.get_secret_value()):
        raise SystemExit("--gemini requires GEMINI_API_KEY in the backend environment.")
    app = create_app()
    decisions = ContextVar("decisions")

    def capture_service():
        service = get_question_answer_service(app.state.db, get_settings(), None)
        if not args.gemini:
            service.generator = GateOnly()
        prepare = service.preparation.prepare

        async def capture(*args, **kwargs):
            decision = await prepare(*args, **kwargs)
            decisions.get().append(decision)
            return decision

        service.preparation.prepare = capture
        return service

    app.dependency_overrides[get_question_answer_service] = capture_service
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    concurrency = asyncio.Semaphore(args.parallel)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        async def run_case(case):
            async with concurrency:
                context = {"context": case["context"]} if "context" in case else {}
                started = perf_counter()
                response = await client.post("/search/legal", json={"query": case["question"], "limit": 10, **context})
                body = response.json()
                result = dict(case, search_seconds=round(perf_counter() - started, 3),
                              search_http=response.status_code, search_status=body.get("status"),
                              publication_id=body.get("publication_id"), warnings=body.get("warnings", []),
                              retrieval=candidate_rows(body.get("results", [])))
                result["expected_gate"] = result.pop("gate", None)
                if result["expected_gate"] or args.all_answers:
                    captured = []
                    token = decisions.set(captured)
                    try:
                        started = perf_counter()
                        answer = await client.post("/ask", json={"question": case["question"], **context})
                    finally:
                        decisions.reset(token)
                    result.update(ask_seconds=round(perf_counter() - started, 3),
                                  ask_http=answer.status_code, answer=answer.json())
                    decision = captured[-1] if captured else None
                    result["gate"] = decision.outcome if decision else None
                    result["gate_reason"] = decision.reason if decision else None
                    result["limited_source_context"] = bool(getattr(decision, "limited_source_context", False))
                    if decision and decision.context:
                        reranked = decision.context.reranked
                        result["retained"] = candidate_rows(
                            [row.candidate.model_dump(mode="json") for row in reranked.candidates])
                        ranks = reranked.all_scores
                        ranked = sorted(reranked.retrieval.results, key=lambda row: (
                            row.exact_rank is None, row.exact_rank or 0, -ranks.get(row.chunk_id, 0), row.rank))
                        result["reranked"] = candidate_rows([row.model_dump(mode="json") for row in ranked])
                        result["context_issues"] = [issue.model_dump(mode="json") for issue in decision.context.issues]
                print(case["id"], result["search_status"], result.get("gate"), result.get("gate_reason"), flush=True)
                return result

        results = await asyncio.gather(*(run_case(case) for case in cases
                                         if not args.only or case["id"] in args.only))
    report = {"mode": "gemini" if args.gemini else "gate_only", "parallel": args.parallel,
              "cases": results, "metrics": summary(results)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("data/rag_regression_cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--all-answers", action="store_true")
    parser.add_argument("--parallel", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--gemini", action="store_true", help="Send prepared prompts to the configured Gemini API")
    asyncio.run(run(parser.parse_args()))
