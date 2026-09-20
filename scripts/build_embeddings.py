"""Fill `law_passages.embedding` with sentence-transformers vectors.

    python scripts/build_embeddings.py              # embed what is missing
    python scripts/build_embeddings.py --recompute  # re-embed everything
    python scripts/build_embeddings.py --dry-run    # report only

Run this after `alembic upgrade head` and
`python scripts/ingest_contract_act_corpus.py`.

The passage text is embedded together with its section heading, so a question
like "what is a voidable contract" can match a passage whose body never repeats
the heading. The model name is written next to every vector: search skips rows
produced by any other model, so a model change can never mix vector spaces.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.services.embeddings import EmbeddingService  # noqa: E402

SELECT_SQL = """
    SELECT p.evidence_id, p.text, s.section_number, s.title, src.law_name
    FROM law_passages p
    JOIN law_sections s ON s.id = p.section_id
    JOIN law_sources src ON src.id = s.source_id
    {where}
    ORDER BY s.section_number, p.page_number
"""

UPDATE_SQL = """
    UPDATE law_passages
    SET embedding = CAST(CAST(:embedding AS text) AS vector),
        embedding_model = :embedding_model
    WHERE evidence_id = :evidence_id
"""


def embedding_input(law_name: str, section_number: int, title: str, body: str) -> str:
    """The exact string that gets embedded. Keep this stable across runs."""
    return f"{law_name}, section {section_number}: {title}\n{body}".strip()


async def main(recompute: bool, dry_run: bool, batch_size: int) -> None:
    settings = get_settings()
    embeddings = EmbeddingService(
        settings.embedding_model,
        dimension=settings.embedding_dim,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        enabled=True,
    )

    where = (
        ""
        if recompute
        else "WHERE p.embedding IS NULL OR p.embedding_model IS DISTINCT FROM :model"
    )
    params = {} if recompute else {"model": settings.embedding_model}

    db = Database(settings)
    try:
        async with db.session() as session:
            rows = (await session.execute(text(SELECT_SQL.format(where=where)), params)).all()
            total = len(rows)
            print(f"Passages to embed: {total} (model: {settings.embedding_model})")
            if not total or dry_run:
                return

            print("Loading model (first run downloads it, ~90 MB)...")
            done = 0
            for start in range(0, total, batch_size):
                batch = rows[start : start + batch_size]
                vectors = await embeddings.embed_texts(
                    [
                        embedding_input(r.law_name, r.section_number, r.title, r.text)
                        for r in batch
                    ]
                )
                for row, vector in zip(batch, vectors, strict=True):
                    await session.execute(
                        text(UPDATE_SQL),
                        {
                            "embedding": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                            "embedding_model": settings.embedding_model,
                            "evidence_id": row.evidence_id,
                        },
                    )
                # One commit per batch: a crash halfway leaves finished batches
                # saved, and re-running only picks up what is still missing.
                await session.commit()
                done += len(batch)
                print(f"  embedded {done}/{total}")

            remaining = (
                await session.execute(
                    text("SELECT count(*) FROM law_passages WHERE embedding IS NULL")
                )
            ).scalar_one()
            print(f"Done. Passages still without an embedding: {remaining}")
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recompute", action="store_true", help="re-embed every passage")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    asyncio.run(main(args.recompute, args.dry_run, args.batch_size))
