"""Source-note markers must remain exact, local to a source, and unambiguous."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.repositories.legal_structure_repository import (
    LegalStructureRepository, StructureClaim, StructureConflict,
)
from app.services.legal_structure import DraftNode, DraftSpan, SourceBlock, SourceDocument, SourcePage


class NoDatabaseAccess:
    def session(self):
        raise AssertionError("Validation passed; database access would begin here")


def example():
    file_id, extraction_id, page_id, marker_block_id, note_block_id = (uuid4() for _ in range(5))
    operative = "9[(da) Organizer means a person."
    note_text = "9Inserted by Amendment Act."
    marker_block = SourceBlock(marker_block_id, page_id, extraction_id, file_id,
                               1, 1, "paragraph", operative, None, 800)
    note_block = SourceBlock(note_block_id, page_id, extraction_id, file_id,
                             1, 2, "footnote", note_text, None, 800)
    document = SourceDocument(file_id, extraction_id, "Example Act", "act", 1, (
        SourcePage(page_id, extraction_id, file_id, 1, "extracted", [], 800,
                   (marker_block, note_block)),
    ))
    target = DraftNode(uuid4(), None, "paragraph", "da", "/s:2/p:da", 1, None, operative)
    note = DraftNode(uuid4(), None, "other", "9", "/note:9", 2, "Source note 9", note_text)
    spans = [
        DraftSpan(target.id, extraction_id, page_id, marker_block_id,
                  0, len(operative), 0, len(operative)),
        DraftSpan(note.id, extraction_id, page_id, note_block_id,
                  0, len(note_text), 0, len(note_text)),
    ]
    link = SimpleNamespace(note_provision_id=note.id, target_provision_id=target.id,
                           marker_label="9", extraction_id=extraction_id,
                           page_id=page_id, block_id=marker_block_id,
                           block_char_start=0, block_char_end=1)
    parsed = SimpleNamespace(nodes=[target, note], spans=spans,
                             decisions=[object(), object()], note_links=[link])
    return parsed, document


@pytest.mark.anyio
async def test_valid_marker_reaches_persistence_after_exact_validation():
    parsed, document = example()
    repo = LegalStructureRepository(NoDatabaseAccess())
    with pytest.raises(AssertionError, match="Validation passed"):
        await repo.complete(StructureClaim(uuid4(), uuid4(), uuid4()), parsed, [document])


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    lambda parsed: setattr(parsed.note_links[0], "block_char_start", 1),
    lambda parsed: setattr(parsed.note_links[0], "block_char_end", 99),
    lambda parsed: setattr(parsed.note_links[0], "marker_label", "8"),
    lambda parsed: setattr(parsed.note_links[0], "page_id", uuid4()),
    lambda parsed: setattr(parsed.note_links[0], "target_provision_id", uuid4()),
    lambda parsed: setattr(parsed.note_links[0], "note_provision_id", uuid4()),
    lambda parsed: parsed.spans.pop(),
    lambda parsed: parsed.note_links.append(SimpleNamespace(**vars(parsed.note_links[0]))),
])
async def test_invalid_marker_links_are_rejected_before_any_database_write(mutation):
    parsed, document = example()
    mutation(parsed)
    repo = LegalStructureRepository(NoDatabaseAccess())
    with pytest.raises(StructureConflict):
        await repo.complete(StructureClaim(uuid4(), uuid4(), uuid4()), parsed, [document])
