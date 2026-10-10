"""Unit tests for deterministic contract extraction and page provenance."""

from __future__ import annotations

import hashlib
from pathlib import Path

import fitz
import pytest

from app.core.errors import ContractExtractionError
from app.services.contract_extraction import (
    ContractExtractionService,
    ExtractionSource,
    StructuredBlock,
    extract_contract,
    segment_clauses,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_pdf(path: Path, page_texts: list[str]) -> bytes:
    pdf = fitz.open()
    for text in page_texts:
        page = pdf.new_page()
        page.insert_text((72, 72), text)
    data = pdf.tobytes()
    pdf.close()
    path.write_bytes(data)
    return data


class FakeEngine:
    name = "fake-structured"
    version = "1.0"

    def __init__(self, blocks: list[StructuredBlock], error: Exception | None = None):
        self.blocks = blocks
        self.error = error

    def extract_blocks(self, pdf_path: Path) -> list[StructuredBlock]:
        if self.error:
            raise self.error
        assert pdf_path.is_file()
        return list(self.blocks)


def test_extract_preserves_pages_headings_tables_and_spanning_clause(tmp_path):
    pdf_path = tmp_path / "agreement.pdf"
    _write_pdf(
        pdf_path,
        [
            "SERVICE AGREEMENT\n1. Services\nProvider will deliver design work.",
            "Services continue after review.\n2. Payment\nClient pays 1000 PKR.",
        ],
    )
    engine = FakeEngine(
        [
            StructuredBlock("title", "SERVICE AGREEMENT", (1,)),
            StructuredBlock("section_header", "1. Services", (1,)),
            StructuredBlock("text", "Provider will deliver design work.", (1,)),
            StructuredBlock("table", "| Milestone | Due |\n| --- | --- |\n| Draft | Day 5 |", (1,)),
            StructuredBlock("text", "Services continue after review.", (2,)),
            StructuredBlock("section_header", "2. Payment", (2,)),
            StructuredBlock("text", "Client pays 1000 PKR.", (2,)),
        ]
    )

    result = extract_contract(pdf_path, document_version_id="docver:test", engine=engine)

    assert len(result.pages) == 2
    assert "SERVICE AGREEMENT" in result.pages[0].text
    assert len(result.clauses) == 3
    services = result.clauses[1]
    assert services.label == "1."
    assert services.heading == "Services"
    assert services.page_start == 1
    assert services.page_end == 2
    assert {span.page_number for span in services.spans} == {1, 2}
    assert "| Milestone | Due |" in services.text
    assert result.clauses[2].label == "2."


def test_clause_ids_are_stable_within_a_version():
    blocks = [StructuredBlock("section_header", "1. Scope", (1,))]
    first = segment_clauses(blocks, version_id="docver:one")
    second = segment_clauses(blocks, version_id="docver:one")
    other_version = segment_clauses(blocks, version_id="docver:two")
    assert first[0].clause_id == second[0].clause_id
    assert first[0].clause_id != other_version[0].clause_id


def test_numbered_text_item_keeps_original_clause_label():
    clauses = segment_clauses(
        [
            StructuredBlock("text", "3.2) Early Termination", (1,)),
            StructuredBlock("text", "Either party may terminate with notice.", (1,)),
        ],
        version_id="docver:test",
    )
    assert clauses[0].label == "3.2)"
    assert clauses[0].heading == "Early Termination"


def test_out_of_range_docling_page_is_rejected(tmp_path):
    pdf_path = tmp_path / "one-page.pdf"
    _write_pdf(pdf_path, ["1. Scope\nSome text"])
    engine = FakeEngine([StructuredBlock("section_header", "1. Scope", (2,))])
    with pytest.raises(ContractExtractionError, match="outside the PDF"):
        extract_contract(pdf_path, document_version_id="docver:test", engine=engine)


class FakeExtractionRepository:
    def __init__(self, source: ExtractionSource):
        self.source = source
        self.calls: list[str] = []
        self.saved = None
        self.failure = None

    async def get_extraction_source(self, version_id: str):
        self.calls.append("get")
        return self.source if version_id == self.source.version_id else None

    async def mark_extracting(self, version_id: str):
        self.calls.append("extracting")

    async def replace_extraction(self, extraction):
        self.calls.append("save")
        self.saved = extraction

    async def mark_extraction_failed(self, version_id: str, message: str):
        self.calls.append("failed")
        self.failure = message


@pytest.mark.anyio
async def test_service_records_failure_when_stored_hash_changed(tmp_path):
    pdf_path = tmp_path / "changed.pdf"
    data = _write_pdf(pdf_path, ["1. Scope\nSome selectable contract text"])
    source = ExtractionSource(
        document_id="doc:test",
        version_id="docver:test",
        storage_path=str(pdf_path),
        pdf_sha256="0" * 64,
        page_count=1,
        status="uploaded",
    )
    repository = FakeExtractionRepository(source)
    engine = FakeEngine([StructuredBlock("section_header", "1. Scope", (1,))])

    assert hashlib.sha256(data).hexdigest() != source.pdf_sha256
    with pytest.raises(ContractExtractionError, match="hash does not match"):
        await ContractExtractionService(repository, engine).extract(source.version_id)

    assert repository.calls == ["get", "extracting", "failed"]
    assert "hash does not match" in repository.failure


@pytest.mark.anyio
async def test_service_saves_successful_extraction(tmp_path):
    pdf_path = tmp_path / "valid.pdf"
    data = _write_pdf(pdf_path, ["1. Scope\nSome selectable contract text"])
    source = ExtractionSource(
        document_id="doc:test",
        version_id="docver:test",
        storage_path=str(pdf_path),
        pdf_sha256=hashlib.sha256(data).hexdigest(),
        page_count=1,
        status="uploaded",
    )
    repository = FakeExtractionRepository(source)
    engine = FakeEngine(
        [
            StructuredBlock("section_header", "1. Scope", (1,)),
            StructuredBlock("text", "Some selectable contract text", (1,)),
        ]
    )

    result = await ContractExtractionService(repository, engine).extract(source.version_id)

    assert repository.calls == ["get", "extracting", "save"]
    assert repository.saved == result
    assert result.engine_name == "fake-structured"
