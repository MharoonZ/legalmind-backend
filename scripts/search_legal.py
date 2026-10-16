"""Query the same Stage 10 service used by POST /search/legal."""
import argparse
import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_search_repository import LegalSearchRepository
from app.schemas.legal_search import LegalReferenceIn, LegalSearchIn, SearchContextIn
from app.services.legal_retrieval import LegalRetrievalService


async def run(args):
    db = Database(get_settings())
    try:
        request = LegalSearchIn(query=args.query, limit=args.limit, instrument_id=args.instrument_id,
            law=args.law, jurisdiction_code=args.jurisdiction,
            reference=LegalReferenceIn(kind=args.kind, label=args.reference) if args.reference else None,
            context=SearchContextIn(previous_query=args.previous_query,
                instrument_id=args.context_instrument_id) if args.previous_query or args.context_instrument_id else None)
        service = LegalRetrievalService(LegalSearchRepository(db), get_settings())
        result = await service.search(request)
        print(result.model_dump_json(indent=2))
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--instrument-id", type=UUID)
    parser.add_argument("--law")
    parser.add_argument("--jurisdiction")
    parser.add_argument("--reference")
    parser.add_argument("--kind", choices=["section", "rule", "article", "schedule"], default="section")
    parser.add_argument("--previous-query")
    parser.add_argument("--context-instrument-id", type=UUID)
    asyncio.run(run(parser.parse_args()))
