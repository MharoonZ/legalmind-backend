"""End-to-end evaluation of POST /ask against labelled questions.

By default the answer pipeline runs in this process against the configured
database and answer model (real retrieval, real model calls). With ``--url``
the same cases are sent to a running server over HTTP instead.

    python -m scripts.eval.evaluate_ask
    python -m scripts.eval.evaluate_ask --url http://127.0.0.1:8000 --output report.json
    python -m scripts.eval.evaluate_ask --only reported-misspelt-law --show-answers

Exit status is 1 when any case fails, so the command can gate a release.
A pass means the expected provision was cited with an allowed status; it is a
regression check, not a certification of legal accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "data" / "ask_evaluation.json"


def judge(case: dict, response: dict) -> tuple[bool, str]:
    status = response.get("status")
    if status not in case["status"]:
        return False, f"status {status!r} not in {case['status']}"
    citations = response.get("citations") or []
    if case.get("no_citations") and citations:
        return False, "unexpected citations"
    expected = case.get("expected") or []
    if expected and status in {"answered", "partial"}:
        cited = {(item["law"], item.get("label") or "") for item in citations}
        if not any(fragment in law and label == wanted for law, label in cited for fragment, wanted in expected):
            return False, "expected provision not cited: " + ", ".join(sorted(
                f"{law[:28]} s.{label}" for law, label in cited)) if cited else "no citations"
    if not expected and status == "answered" and not case.get("no_citations") and citations and \
            "answered" not in case["status"]:
        return False, "answered a question the collection should not cover"
    return True, ""


async def _in_process(cases: list[dict]):
    from app.bootstrap import build_services
    from app.core.config import get_settings
    from app.db.session import Database
    from app.schemas.qa import AskIn

    settings = get_settings()
    db = Database(settings)
    if not await db.connect():
        raise SystemExit("The database is unavailable.")
    services = build_services(db, settings)
    await services.index.refresh()
    if services.index.snapshot is None:
        raise SystemExit("No active publication could be loaded.")
    await services.warm()
    try:
        for case in cases:
            started = perf_counter()
            result, timings = await services.qa.ask(AskIn(question=case["question"], history=case.get("history", [])))
            yield case, result.model_dump(mode="json"), perf_counter() - started, timings
    finally:
        await services.aclose()
        await db.dispose()


async def _over_http(cases: list[dict], url: str, api_key: str | None):
    import httpx
    headers = {"X-API-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(base_url=url, timeout=120, headers=headers) as client:
        for case in cases:
            started = perf_counter()
            response = await client.post("/ask", json={"question": case["question"],
                                                         "history": case.get("history", [])})
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            yield case, body, perf_counter() - started, {}


async def run(args) -> int:
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [case for case in cases if case["id"] in args.only or case["category"] in args.only]
    source = _over_http(cases, args.url, args.api_key) if args.url else _in_process(cases)
    rows, failures = [], 0
    async for case, response, seconds, timings in source:
        passed, reason = judge(case, response)
        failures += not passed
        rows.append({"id": case["id"], "category": case["category"], "passed": passed, "reason": reason,
                     "status": response.get("status"), "seconds": round(seconds, 2),
                     "citations": [f"{item['law']} | {item['kind']} {item.get('label')} | p{item['page_index']}"
                                   for item in response.get("citations") or []],
                     "timings_ms": {key: round(value) for key, value in timings.items()},
                     "question": case["question"], "answer": response.get("answer")})
        print(f"{'PASS' if passed else 'FAIL'}  {seconds:6.2f}s  {str(response.get('status')):22s} "
              f"{case['id']}" + (f"  <- {reason}" if reason else ""), flush=True)
        if args.show_answers:
            print("        Q:", case["question"])
            print("        A:", (response.get("answer") or "").replace("\n", "\n           ")[:1500])
    answered = [row["seconds"] for row in rows if row["status"] in {"answered", "partial"}]
    summary = {
        "cases": len(rows), "passed": len(rows) - failures, "failed": failures,
        "median_seconds_answered": round(statistics.median(answered), 2) if answered else None,
        "p90_seconds_answered": round(sorted(answered)[int(len(answered) * 0.9) - 1], 2) if answered else None,
        "max_seconds": max((row["seconds"] for row in rows), default=None),
        "by_category": {},
    }
    for row in rows:
        bucket = summary["by_category"].setdefault(row["category"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += row["passed"]
    print(json.dumps(summary, indent=2))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps({"summary": summary, "results": rows}, indent=2,
                                                ensure_ascii=False), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--url", help="Base URL of a running server; omit to run in process")
    parser.add_argument("--api-key", help="X-API-Key for a server that requires one")
    parser.add_argument("--only", nargs="*", help="Case ids or categories to run")
    parser.add_argument("--output", help="Write the full JSON report here")
    parser.add_argument("--show-answers", action="store_true")
    sys.exit(asyncio.run(run(parser.parse_args())))
