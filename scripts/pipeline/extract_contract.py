"""Extract one uploaded contract version, or preview a PDF without database writes.

Database mode (normal pipeline):
    python scripts/extract_contract.py --version-id docver:<id>

Preview mode (Day 1 handoff / debugging):
    python scripts/extract_contract.py --pdf path/to/sample.pdf

The JSON printed by either mode contains clause IDs, original labels, text,
and page mappings. Preview mode never stores the PDF or extraction output.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.repositories.contract_extraction_repository import (  # noqa: E402
    ContractExtractionRepository,
)
from app.services.contract_extraction import (  # noqa: E402
    ContractExtractionService,
    DoclingExtractionEngine,
    ExtractedContract,
    extract_contract,
)


def _output(extraction: ExtractedContract) -> None:
    # Windows terminals often default to cp1252, while contracts may contain
    # arrows, smart punctuation, or other Unicode text. Keep preview output
    # lossless instead of escaping or failing at the final print.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    payload = {
        "version_id": extraction.document_version_id,
        "engine": {
            "name": extraction.engine_name,
            "version": extraction.engine_version,
        },
        "extracted_text_sha256": extraction.extracted_text_sha256,
        "page_count": len(extraction.pages),
        "clauses": [
            {
                "clause_id": clause.clause_id,
                "ordinal": clause.ordinal,
                "label": clause.label,
                "heading": clause.heading,
                "pages": sorted({span.page_number for span in clause.spans}),
                "page_start": clause.page_start,
                "page_end": clause.page_end,
                "text": clause.text,
            }
            for clause in extraction.clauses
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def _database_extract(version_id: str) -> None:
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.session() as session:
            repository = ContractExtractionRepository(session)
            engine = DoclingExtractionEngine(
                max_pages=settings.upload_max_pages,
                max_file_size=settings.upload_max_size_bytes,
            )
            result = await ContractExtractionService(
                repository, engine, upload_root=settings.upload_dir).extract(version_id)
            _output(result)
    finally:
        await database.dispose()


def _preview(pdf_path: Path) -> None:
    resolved = pdf_path.resolve(strict=True)
    version_id = "preview:" + hashlib.sha256(resolved.read_bytes()).hexdigest()[:16]
    result = extract_contract(
        resolved,
        document_version_id=version_id,
        engine=DoclingExtractionEngine(),
    )
    _output(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--version-id", help="existing document_versions.id to extract")
    mode.add_argument("--pdf", type=Path, help="preview one local PDF without database writes")
    args = parser.parse_args()
    if args.pdf is not None:
        _preview(args.pdf)
    else:
        asyncio.run(_database_extract(args.version_id))
