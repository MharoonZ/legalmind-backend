"""Import the draft corpus after running ``alembic upgrade head``.

    python scripts/ingest_contract_act_corpus.py

This script verifies both the JSON's internal hashes and the actual PDF bytes.
It inserts rows with ``needs_review`` and deliberately does not generate
embeddings or approve the source.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.corpus.models import CorpusVersion  # noqa: E402
from app.corpus.repository import CorpusRepository  # noqa: E402
from app.db.session import Database  # noqa: E402


async def main(check_only: bool = False) -> None:
    pdf = ROOT / "data" / "sources" / "contract_act_1872_pakistan_code.pdf"
    corpus_file = ROOT / "data" / "contract_act_1872_sections_1_30.json"
    corpus = CorpusVersion.from_dict(json.loads(corpus_file.read_text(encoding="utf-8")))
    actual_pdf_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if actual_pdf_sha != corpus.pdf_sha256:
        raise ValueError("Local PDF bytes do not match the corpus manifest")
    if check_only:
        print(
            f"Verified {len(corpus.sections)} draft sections and "
            f"{sum(len(section.passages) for section in corpus.sections)} passages"
        )
        print(f"PDF SHA-256: {actual_pdf_sha}")
        print(f"Corpus version: {corpus.source_id}")
        return

    db = Database(get_settings())
    try:
        async with db.session() as session:
            async with session.begin():
                inserted = await CorpusRepository(session).import_version(corpus)
        print("Inserted draft corpus" if inserted else "Draft corpus already present")
        print(f"Source ID: {corpus.source_id}")
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verify JSON and PDF without connecting to PostgreSQL",
    )
    asyncio.run(main(check_only=parser.parse_args().check_only))
