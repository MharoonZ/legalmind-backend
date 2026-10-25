"""Check the configured database/publication without changing shared data."""
import asyncio

from sqlalchemy import select, text

from app.core.config import get_settings
from app.db.publication_models import CorpusPublication
from app.db.session import Database
from app.repositories.legal_search_repository import LegalSearchRepository
from app.schemas.legal_search import LegalSearchIn
from app.services.legal_retrieval import LegalRetrievalService


async def main():
    db = Database(get_settings())
    try:
        async with db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            publication = await session.scalar(select(CorpusPublication).where(
                CorpusPublication.status == "active"))
            if publication is None:
                print("Database reachable; no active publication.")
            else:
                print("Database reachable; active publication:", publication.name)
                print("Published chunk count:", publication.chunk_count)
        retrieval = LegalRetrievalService(LegalSearchRepository(db), get_settings())
        outside = await retrieval.search(LegalSearchIn(
            query="Section 9999 of the Imaginary Act", limit=3))
        print("Out-of-corpus exact lookup:", outside.status)
    except Exception as exc:
        # Do not print SQL exception text: it can include connection details.
        print("Read-only database check failed:", type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
