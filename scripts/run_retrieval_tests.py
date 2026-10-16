"""Run the 6 key retrieval verification queries in one process and save the results.

This script executes all 6 test queries against the active legal publication,
loads the BGE-M3 embedding model only ONCE in memory, prints a clean summary
to the console, and saves the full structured JSON output to
`backups/retrieval_test_results.json` (or custom path via --output).

Usage:
    .\\.venv\\Scripts\\python -m scripts.run_retrieval_tests
    .\\.venv\\Scripts\\python -m scripts.run_retrieval_tests --output my_test_results.json
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_search_repository import LegalSearchRepository
from app.schemas.legal_search import LegalSearchIn, SearchContextIn
from app.services.legal_retrieval import LegalRetrievalService

TEST_CASES = [
    {
        "id": "1_exact_section",
        "description": "Exact section reference with Act name",
        "query": "Section 19 of the Consumer Protection Act",
        "limit": 3,
        "context": None,
    },
    {
        "id": "2_exact_child_clause",
        "description": "Exact child paragraph reference (clause precision)",
        "query": "Section 4(2)(a) of the Consumer Protection Act",
        "limit": 3,
        "context": None,
    },
    {
        "id": "3_semantic_paraphrase",
        "description": "Natural language semantic query (BGE-M3 embeddings + RRF)",
        "query": "The shopkeeper refused to give me proof of purchase",
        "limit": 3,
        "context": None,
    },
    {
        "id": "4_vagrancy_child_begging",
        "description": "Semantic search for vagrancy/destitute children",
        "query": "penalty for child begging",
        "limit": 3,
        "context": None,
    },
    {
        "id": "5_conversational_followup",
        "description": "Follow-up query with conversational context",
        "query": "What if they refuse to issue it?",
        "limit": 3,
        "context": {
            "previous_query": "Must a seller give a receipt under the Punjab Consumer Protection Act 2005?"
        },
    },
    {
        "id": "6_out_of_domain_negative",
        "description": "Negative out-of-domain test (expected: no results)",
        "query": "How does quantum entanglement work in a qubit?",
        "limit": 3,
        "context": None,
    },
]


async def run(output_path: str):
    settings = get_settings()
    db = Database(settings)
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("LegalMind Retrieval Test Suite")
    print("=" * 70)
    print(f"Connecting to database and initializing service...")

    try:
        service = LegalRetrievalService(LegalSearchRepository(db), settings)
        all_results = []

        for idx, tc in enumerate(TEST_CASES, start=1):
            print(f"\n[{idx}/6] Running: {tc['description']}")
            print(f"      Query: \"{tc['query']}\"")
            if tc["context"]:
                print(f"      Context: \"{tc['context']['previous_query']}\"")

            t0 = time.perf_counter()
            ctx = SearchContextIn(**tc["context"]) if tc["context"] else None
            req = LegalSearchIn(query=tc["query"], limit=tc["limit"], context=ctx)
            resp = await service.search(req)
            elapsed = time.perf_counter() - t0

            resp_dict = json.loads(resp.model_dump_json())

            # Print concise summary
            top_hit = resp_dict.get("results", [])[0] if resp_dict.get("results") else None
            status = resp_dict.get("status")
            print(f"      Status: {status} ({elapsed:.2f}s)")
            if top_hit:
                print(f"      Top Hit [Rank 1]: {top_hit.get('instrument_title')} — Section/Rule: {top_hit.get('label')}")
                print(f"      Reference Path: {top_hit.get('reference_path')}")
                if top_hit.get("rrf_rank") is not None:
                    print(f"      Scores: Keyword Rank={top_hit.get('keyword_rank')}, Vector Rank={top_hit.get('vector_rank')}, RRF Rank={top_hit.get('rrf_rank')}")
                elif top_hit.get("exact_rank") is not None:
                    print(f"      Scores: Exact Citation Match (Rank 1)")
            else:
                msg = resp_dict.get("message", "No matching legal provisions found.")
                print(f"      Outcome: {msg}")

            all_results.append({
                "test_id": tc["id"],
                "description": tc["description"],
                "input": {
                    "query": tc["query"],
                    "limit": tc["limit"],
                    "context": tc["context"],
                },
                "execution_seconds": round(elapsed, 3),
                "response": resp_dict,
            })

        # Save to output file
        out_file.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
        print("\n" + "=" * 70)
        print(f"SUCCESS! All 6 test queries completed.")
        print(f"Full response saved to: {out_file.resolve()}")
        print("=" * 70)

    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="backups/retrieval_test_results.json",
        help="Path where formatted JSON results will be written (default: backups/retrieval_test_results.json)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.output))

