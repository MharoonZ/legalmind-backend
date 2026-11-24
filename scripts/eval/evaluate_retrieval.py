"""Evaluate first-stage retrieval (no answer model) against labelled questions.

    python -m scripts.eval.evaluate_retrieval
    python -m scripts.eval.evaluate_retrieval --output report.json --min-similarity 0.35

Uses every case in ``data/ask_evaluation.json`` that names an expected
provision and reports where that provision ranks among the fused candidates
(hit@1/3/5 and MRR). Exit status is 1 when hit@5 falls below ``--require``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "data" / "ask_evaluation.json"


async def run(args) -> int:
    from app.bootstrap import build_services
    from app.core.config import get_settings
    from app.db.session import Database

    settings = get_settings()
    if args.min_similarity is not None:
        settings = settings.model_copy(update={"legal_search_min_similarity": args.min_similarity})
    cases = [case for case in json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
             if case.get("expected")]
    db = Database(settings)
    if not await db.connect():
        raise SystemExit("The database is unavailable.")
    services = build_services(db, settings)
    rows = []
    try:
        await services.index.refresh()
        snapshot = services.index.snapshot
        if snapshot is None:
            raise SystemExit("No active publication could be loaded.")
        await services.retrieval.encoder.warm(snapshot.profile)
        for case in cases:
            history = [(turn["role"], turn["content"]) for turn in case.get("history", [])]
            started = perf_counter()
            result = await services.retrieval.retrieve(case["question"], history=history)
            elapsed = (perf_counter() - started) * 1000
            rank, roots = None, []
            for candidate in result.candidates:
                key = (candidate.chunk.instrument_id, candidate.chunk.root_provision_id)
                if key not in roots:
                    roots.append(key)
                title = snapshot.instruments[candidate.chunk.instrument_id].title
                if any(fragment in title and (candidate.chunk.label or "") == label
                       for fragment, label in case["expected"]):
                    rank = len(roots)
                    break
            rows.append({"id": case["id"], "category": case["category"], "status": result.status,
                         "rank": rank, "milliseconds": round(elapsed, 1),
                         "mode": "exact" if result.analysis and result.analysis.has_reference else "hybrid"})
            print(f"{'-' if rank is None else rank:>3}  {elapsed:7.1f} ms  {result.status:20s} {case['id']}", flush=True)
    finally:
        await services.aclose()
        await db.dispose()
    total = len(rows) or 1
    summary = {"cases": len(rows), "min_similarity": settings.legal_search_min_similarity,
               **{f"hit_at_{k}": round(sum(row["rank"] is not None and row["rank"] <= k for row in rows) / total, 3)
                  for k in (1, 3, 5)},
               "mrr": round(sum(1 / row["rank"] for row in rows if row["rank"]) / total, 3),
               "median_ms": sorted(row["milliseconds"] for row in rows)[len(rows) // 2] if rows else None}
    print(json.dumps(summary, indent=2))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps({"summary": summary, "results": rows}, indent=2), encoding="utf-8")
    return 0 if summary["hit_at_5"] >= args.require else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--min-similarity", type=float)
    parser.add_argument("--require", type=float, default=0.9, help="Minimum hit@5 for a zero exit status")
    parser.add_argument("--output")
    sys.exit(asyncio.run(run(parser.parse_args())))
