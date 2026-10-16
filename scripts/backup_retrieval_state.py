"""Consistent, local safety snapshot of retrieval tables, not a full database backup.

Does not print connection details or vector payloads. Keep backups private.
"""
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.session import Database
from app.db.streaming import fetch_rows


async def snapshot_once():
    db = Database(get_settings())
    try:
        async with db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            available = set((await session.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))).all())
            snapshot = {}
            for table in ("alembic_version", "chunk_sets", "legal_chunks", "legal_chunk_provisions",
                          "embedding_profiles", "legal_chunk_embeddings", "corpus_publications", "publication_items"):
                if table in available:
                    print(json.dumps({"event": "snapshot_table", "table": table}), file=sys.stderr, flush=True)
                    rows = await fetch_rows(session, text(f"SELECT * FROM {table}"), mappings=True, batch_size=50)
                    snapshot[table] = [dict(r) for r in rows]
            snapshot["indexes"] = [dict(r) for r in (await session.execute(text("""
                SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public'
                  AND tablename IN ('legal_chunks','legal_chunk_embeddings','corpus_publications','publication_items')
            """))).mappings().all()]
        data = json.dumps(snapshot, default=str, ensure_ascii=False, sort_keys=True).encode("utf-8")
        destination = Path("backups") / ("retrieval_state_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as output:
            output.write(data)
        print(json.dumps({"file": str(destination.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
                          "rows": {k: len(v) for k, v in snapshot.items()}}))
    finally:
        await db.dispose()


if __name__ == "__main__":
    async def main():
        for attempt in range(3):
            try:
                await snapshot_once()
                return
            except DBAPIError as exc:
                if not exc.connection_invalidated or attempt == 2:
                    raise
                await asyncio.sleep(1)
    asyncio.run(main())
