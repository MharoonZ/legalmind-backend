"""Export all three derived chunk tables to a local, ignored JSON backup.

This is a safety snapshot before the additive v2 migration; it does not
substitute for a full PostgreSQL backup of the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database


async def main() -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable")
        tables = ("chunk_sets", "legal_chunks", "legal_chunk_provisions")
        async with db.session() as session:
            snapshot = {}
            for table in tables:
                rows = (await session.execute(text(f"SELECT * FROM {table}"))).mappings().all()
                snapshot[table] = [dict(row) for row in rows]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = Path("backups") / f"legal_chunks_{stamp}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(snapshot, default=str, ensure_ascii=False, sort_keys=True).encode("utf-8")
        destination.write_bytes(data)
        print(json.dumps({"file": str(destination.resolve()),
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "rows": {table: len(snapshot[table]) for table in tables}}))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
