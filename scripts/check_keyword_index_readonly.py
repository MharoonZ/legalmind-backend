"""Show PostgreSQL's normal and forced GIN plans for a lexical query."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database


SQL = """EXPLAIN (ANALYZE, BUFFERS)
SELECT id FROM legal_chunks
WHERE search_vector @@ plainto_tsquery('english'::regconfig, :query)
LIMIT 20"""


async def main() -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable")
        async with db.session() as session:
            normal = (await session.execute(text(SQL), {"query": "consumer courts"})).scalars().all()
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            forced = (await session.execute(text(SQL), {"query": "consumer courts"})).scalars().all()
            print(json.dumps({"normal_plan": normal, "forced_index_plan": forced}, indent=2))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
