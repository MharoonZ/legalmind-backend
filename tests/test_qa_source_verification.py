"""Read-only source verifier tested without a live database or PDF changes."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as Row

import pytest
from sqlalchemy.dialects import postgresql

from app.services.qa.citations import CitationVerificationError
from app.services.qa.source_verification import QaSourceVerifier
from tests.qa_fixtures import identity
from tests.test_qa_context import expand, graph


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None
        self.calls = 0

    async def execute(self, statement):
        self.statement = statement
        self.calls += 1
        return Row(all=lambda: self.rows)


class FakeRepository:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []

    @asynccontextmanager
    async def proof_snapshot(self, publication_id, *, chunk_ids, provision_ids):
        assert publication_id == identity("publication")
        self.calls.append((chunk_ids, provision_ids))
        yield self.catalog


def prepared_source(block_text=None):
    catalog, reranked = graph()
    unit = expand(catalog, reranked).units[0]
    node = catalog.nodes[unit.provision_id]
    text = unit.text
    span = Row(id=identity("span-1"), mapping_quality="exact",
               block_char_start=0, block_char_end=len(text),
               provision_char_start=0, provision_char_end=len(text))
    link = Row(chunk_id=unit.chunk_id, chunk_char_start=0, chunk_char_end=len(text),
               provision_char_start=0, provision_char_end=len(text))
    page = Row(page_index=1)
    block = Row(normalized_text=block_text if block_text is not None else text,
                raw_text=block_text if block_text is not None else text)
    file = Row(id=identity("file"), sha256="a" * 64,
               original_filename="synthetic.pdf", source_url=None)
    catalog.session = FakeSession([(link, span, node, page, block, Row(), file, Row())])
    verifier = QaSourceVerifier(None)
    verifier.context_repository = FakeRepository(catalog)
    return verifier, catalog, unit


def test_quote_is_rechecked_against_pinned_provision_and_pdf_block():
    verifier, catalog, unit = prepared_source()
    proofs = asyncio.run(verifier.verify(unit, 0, len(unit.text)))
    assert proofs[0].file_sha256 == "a" * 64
    assert proofs[0].page_index == 1
    compiled = str(catalog.session.statement.compile(dialect=postgresql.dialect()))
    assert "legal_chunk_provisions" in compiled
    assert "revision_sources" in compiled


def test_pdf_block_mismatch_fails_closed():
    verifier, catalog, unit = prepared_source()
    # Keep offsets valid to isolate text identity; range corruption has its own tests.
    catalog.session.rows[0][4].normalized_text = "X" * len(unit.text)
    with pytest.raises(CitationVerificationError, match="source_text_changed"):
        asyncio.run(verifier.verify(unit, 0, len(unit.text)))


def test_quote_cannot_cite_unmapped_display_prefix():
    verifier, catalog, unit = prepared_source()
    link, span, node, page, block, extraction, file, source = catalog.session.rows[0]
    catalog.session.rows = [(Row(chunk_id=unit.chunk_id, chunk_char_start=10, chunk_char_end=len(unit.text),
                                 provision_char_start=10, provision_char_end=len(unit.text)),
                             Row(id=span.id, mapping_quality="exact", block_char_start=10,
                                 block_char_end=len(unit.text), provision_char_start=10,
                                 provision_char_end=len(unit.text)), node, page, block,
                             extraction, file, source)]
    with pytest.raises(CitationVerificationError, match="source_span_gap"):
        asyncio.run(verifier.verify(unit, 0, len(unit.text)))


def test_multiple_claims_share_one_fresh_snapshot_and_one_source_query():
    verifier, catalog, unit = prepared_source()
    requests = [(unit, 0, len(unit.text)), (unit, 4, len(unit.text))] * 3
    results = asyncio.run(verifier.verify_many(requests))
    assert len(results) == 6 and all(results)
    assert verifier.context_repository.calls == [({unit.chunk_id}, set())]
    assert catalog.session.calls == 1


def test_next_answer_rechecks_source_instead_of_reusing_previous_proofs():
    verifier, catalog, unit = prepared_source()
    asyncio.run(verifier.verify(unit, 0, len(unit.text)))
    catalog.session.rows[0][4].normalized_text = "X" * len(unit.text)
    with pytest.raises(CitationVerificationError, match="source_text_changed"):
        asyncio.run(verifier.verify(unit, 0, len(unit.text)))
    assert len(verifier.context_repository.calls) == 2
    assert catalog.session.calls == 2


def test_invalid_later_claim_cannot_reuse_valid_first_claim_proofs():
    verifier, catalog, unit = prepared_source()
    changed = unit.model_copy(update={"text": "A fabricated passage that must not be cited."})
    with pytest.raises(CitationVerificationError, match="source_text_changed"):
        asyncio.run(verifier.verify_many([(unit, 0, 20), (changed, 0, 20)]))
    assert catalog.session.calls == 0


def test_mixed_publications_fail_before_opening_a_snapshot():
    verifier, _, unit = prepared_source()
    other = unit.model_copy(update={"publication_id": identity("other-publication")})
    with pytest.raises(CitationVerificationError, match="wrong_publication"):
        asyncio.run(verifier.verify_many([(unit, 0, 20), (other, 0, 20)]))
    assert verifier.context_repository.calls == []
