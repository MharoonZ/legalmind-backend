"""Print saved source blocks and fresh preview ownership for selected revision pages.

Read-only diagnostic; never persists a parser result.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository
from app.services.legal_structure import parse_structure


async def main(revision_id: UUID, pages: set[int], contains: str | None) -> None:
    db = Database(get_settings())
    if not await db.connect():
        raise RuntimeError("Database unavailable")
    try:
        documents = await LegalStructureRepository(db).load_sources(revision_id)
        preview = parse_structure(documents)
        nodes = {node.id: node for node in preview.nodes}
        for document in documents:
            for page in document.pages:
                if pages and page.page_index not in pages:
                    continue
                shown = False
                for block in page.blocks:
                    if contains and contains.casefold() not in block.text.casefold():
                        continue
                    if not shown:
                        print(f"PAGE {page.page_index} {document.title} height={page.height_pt}")
                        shown = True
                    print(f"BLOCK {block.ordinal} {block.kind} {block.id} {block.bbox}")
                    print(repr(block.text))
                    for span in preview.spans:
                        if span.block_id != block.id:
                            continue
                        node = nodes[span.provision_id]
                        print("  ->", node.reference_path,
                              repr(block.text[span.block_char_start:span.block_char_end]))
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", type=UUID)
    parser.add_argument("pages", nargs="*", type=int)
    parser.add_argument("--contains")
    args = parser.parse_args()
    asyncio.run(main(args.revision, set(args.pages), args.contains))
