"""Query the retrieval service behind POST /search/legal from the command line.

    python -m scripts.audit.search_legal "penalty for gambling in a public place"
    python -m scripts.audit.search_legal "what does it say" --law "gambling ordinance" --reference 5-A

Read-only: loads the active publication and prints the ranked candidates.
"""
import argparse
import asyncio
from uuid import UUID

from app.bootstrap import build_services
from app.core.config import get_settings
from app.db.session import Database
from app.schemas.legal_search import LegalReferenceIn, LegalSearchIn, SearchContextIn


async def run(args) -> int:
    settings = get_settings()
    db = Database(settings)
    services = build_services(db, settings)
    try:
        if not await db.connect():
            raise SystemExit("The database is unavailable.")
        request = LegalSearchIn(
            query=args.query, limit=args.limit, instrument_id=args.instrument_id, law=args.law,
            jurisdiction_code=args.jurisdiction,
            reference=LegalReferenceIn(kind=args.kind, label=args.reference) if args.reference else None,
            context=SearchContextIn(previous_query=args.previous_query, instrument_id=args.context_instrument_id)
            if args.previous_query or args.context_instrument_id else None)
        result = await services.retrieval.search(request)
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            print(f"status: {result.status}   publication: {result.publication_name}")
            for line in (result.message, *result.warnings, *(f"missing: {m}" for m in result.missing_references)):
                if line:
                    print(" ", line)
            for option in result.clarification_options:
                print("  option:", option.get("instrument_title"))
            for row in result.results:
                score = "exact" if row.exact_rank else f"rrf={row.rrf_score:.4f} kw={row.keyword_rank} vec={row.vector_rank}"
                pages = sorted({ref["page_index"] for ref in row.source_refs})
                print(f"{row.rank:>3}. {row.instrument_title} | {row.kind} {row.label or ''} | "
                      f"{row.heading or ''} | pages {pages} | {score}")
        return 0 if result.status in {"ok", "no_results", "needs_clarification"} else 1
    finally:
        await services.aclose()
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--instrument-id", type=UUID)
    parser.add_argument("--law")
    parser.add_argument("--jurisdiction")
    parser.add_argument("--reference")
    parser.add_argument("--kind", choices=["section", "rule", "article", "schedule"], default="section")
    parser.add_argument("--previous-query")
    parser.add_argument("--context-instrument-id", type=UUID)
    parser.add_argument("--json", action="store_true", help="Print the full API response")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
