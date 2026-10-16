"""Evaluate Stage 10 against explicit developer-labeled pilot questions."""
import argparse
import asyncio
import json
import time
from pathlib import Path

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_search_repository import LegalSearchRepository
from app.schemas.legal_search import LegalSearchIn
from app.services.legal_retrieval import LegalRetrievalService


async def run(cases_file, output, min_similarity=None):
    settings = get_settings()
    if min_similarity is not None:
        settings = settings.model_copy(update={"legal_search_min_similarity": min_similarity})
    db = Database(settings)
    try:
        service = LegalRetrievalService(LegalSearchRepository(db), settings)
        dataset = json.loads(Path(cases_file).read_text(encoding="utf-8"))
        results = []
        for case in dataset["cases"]:
            started = time.perf_counter()
            request = LegalSearchIn(query=case["query"], context=case.get("context"), limit=5)
            response = await service.search(request)
            match_rank = next((r.rank for r in response.results
                if case.get("law_contains", "") in r.instrument_title and
                   (not case.get("labels") or r.label in case["labels"])), None)
            source_integrity = all(r.source_refs and all(s.get("file_id") and s.get("source_span_id")
                and s.get("page_index") for s in r.source_refs) for r in response.results)
            passed = response.status == case["status"] and (not case.get("labels") or match_rank is not None) and source_integrity
            item = {"id": case["id"], "query": case["query"], "expected_status": case["status"],
                "actual_status": response.status, "passed": passed, "relevant_rank": match_rank,
                "latency_seconds": round(time.perf_counter()-started, 3),
                "warnings": response.warnings, "publication_id": str(response.publication_id),
                "source_integrity_ok": source_integrity,
                "results": [{"chunk_id": str(r.chunk_id), "law": r.instrument_title,
                    "label": r.label, "reference_path": r.reference_path, "keyword_rank": r.keyword_rank,
                    "vector_rank": r.vector_rank, "rrf_rank": r.rrf_rank,
                    "vector_score": r.vector_score, "rrf_score": r.rrf_score,
                    "source_count": len(r.source_refs)} for r in response.results]}
            results.append(item)
            print(json.dumps({"case": case["id"], "passed": passed, "status": response.status,
                              "relevant_rank": match_rank}), flush=True)
        relevant = [r for r in results if next(c for c in dataset["cases"] if c["id"]==r["id"]).get("labels")]
        report = {"description": dataset["description"], "cases": len(results),
            "passed": sum(r["passed"] for r in results), "min_similarity": settings.legal_search_min_similarity,
            "hit_at_5": sum(r["relevant_rank"] is not None for r in relevant)/len(relevant) if relevant else None,
            "mrr_at_5": sum(1/r["relevant_rank"] if r["relevant_rank"] else 0 for r in relevant)/len(relevant) if relevant else None,
            "semantic_fallback_cases": sum(bool(r["warnings"]) for r in results), "results": results}
        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
        return report["passed"] == len(results) and not report["semantic_fallback_cases"]
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("data/retrieval_evaluation.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-similarity", type=float)
    args = parser.parse_args()
    if args.min_similarity is not None and not 0 <= args.min_similarity <= 1:
        parser.error("--min-similarity must be between 0 and 1")
    raise SystemExit(0 if asyncio.run(run(args.cases, args.output, args.min_similarity)) else 2)
