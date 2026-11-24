"""Physical-page accounting, OCR routing, provenance offsets and worker behavior."""

import hashlib
from pathlib import Path
from uuid import uuid4

import fitz
import pytest

from app.repositories.legal_extraction_repository import ExtractionClaim
from app.services.legal_extraction import (
    ExtractionConfig,
    SourcePdfError,
    extract_page,
    resolve_source_pdf,
    verify_source_pdf,
)
from app.workers.legal_extract import process_one


def _scanned_page() -> fitz.Document:
    source = fitz.open()
    source_page = source.new_page()
    source_page.insert_text((55, 90), "CRIMINAL LAW TEST PAGE", fontsize=20)
    pixmap = source_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    scanned = fitz.open()
    target = scanned.new_page()
    target.insert_image(target.rect, stream=pixmap.tobytes("png"))
    source.close()
    return scanned


def test_native_text_and_block_offsets():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 80), "Section 19A", fontsize=16)
    page.insert_text((50, 120), "The exact source wording is preserved.", fontsize=12)
    result = extract_page(page, 1, ExtractionConfig(tessdata_dir="unused"))
    assert result.outcome == "extracted"
    assert result.extraction_method == "native"
    assert "Section 19A" in result.normalized_text
    assert [block.ordinal for block in result.blocks] == list(range(1, len(result.blocks) + 1))
    for block in result.blocks:
        if block.page_char_start is not None:
            assert result.normalized_text[block.page_char_start:block.page_char_end] == block.normalized_text
            assert block.bbox["x1"] > block.bbox["x0"]
    document.close()


def test_table_rows_columns_and_cell_boxes_are_saved_as_review_candidates():
    document = fitz.open()
    page = document.new_page()
    for x in (50, 200, 350):
        page.draw_line((x, 100), (x, 180))
    for y in (100, 140, 180):
        page.draw_line((50, y), (350, y))
    for x, y, value in ((60, 125, "Provision"), (210, 125, "Penalty"),
                        (60, 165, "Section 498"), (210, 165, "Two years")):
        page.insert_text((x, y), value)
    result = extract_page(page, 1, ExtractionConfig())
    assert result.outcome == "needs_review"
    assert any(item["code"] == "table_layout_candidate" for item in result.warnings)
    table = result.table_regions[0]
    assert (table["row_count"], table["column_count"]) == (2, 2)
    assert table["rows"][1][0]["text"] == "Section 498"
    assert table["rows"][1][1]["text"] == "Two years"
    assert table["rows"][1][0]["bbox"]["x1"] == 200
    assert "Section 498" in result.normalized_text
    document.close()


def test_ambiguous_native_numbering_is_flagged_without_changing_raw_text():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 80), "1. Short title. (l) This Act may be called the Example Act.")
    page.insert_text((50, 110), "(2) It extends to the province.")
    result = extract_page(page, 1, ExtractionConfig())
    assert result.outcome == "needs_review"
    assert "(l)" in result.raw_text and "(l)" in result.normalized_text
    assert any(item["code"] == "ambiguous_numbering_glyph" for item in result.warnings)
    document.close()


def test_textless_page_requires_review_not_automatic_blank():
    document = fitz.open()
    result = extract_page(document.new_page(), 1, ExtractionConfig(tessdata_dir="unused"))
    assert result.outcome == "needs_review"
    assert result.extraction_method == "none"
    assert [item["code"] for item in result.warnings] == ["blank_candidate"]
    document.close()


def test_scanned_page_reports_missing_ocr_data():
    document = _scanned_page()
    result = extract_page(document[0], 1, ExtractionConfig(tessdata_dir="missing-tessdata"))
    assert result.outcome == "failed"
    assert result.normalized_text == ""
    assert any(item["code"] == "ocr_failed" for item in result.warnings)
    assert any(block.kind == "figure" for block in result.blocks)
    document.close()


def test_real_ocr_on_scanned_page_when_language_data_is_available():
    tessdata = Path("uploads/ocr/tessdata")
    if not (tessdata / "eng.traineddata").is_file():
        pytest.skip("Local Tesseract English data is not installed")
    document = _scanned_page()
    result = extract_page(document[0], 1, ExtractionConfig(tessdata_dir=str(tessdata)))
    assert result.extraction_method == "ocr"
    assert result.outcome == "needs_review"
    assert "CRIMINAL LAW TEST PAGE" in result.normalized_text
    assert any(item["code"] == "ocr_unverified" for item in result.warnings)
    assert result.ocr_confidence is None
    assert any(block.kind == "figure" for block in result.blocks)
    document.close()


def test_registered_file_path_and_hash_are_verified(tmp_path):
    data = b"%PDF-test"
    sha = hashlib.sha256(data).hexdigest()
    target = tmp_path / sha[:2] / f"{sha}.pdf"
    target.parent.mkdir()
    target.write_bytes(data)
    path = resolve_source_pdf(str(tmp_path), f"{sha[:2]}/{sha}.pdf", sha)
    verify_source_pdf(path, sha)
    with pytest.raises(SourcePdfError):
        resolve_source_pdf(str(tmp_path), "../../private.pdf", sha)
    target.write_bytes(b"tampered")
    with pytest.raises(SourcePdfError):
        verify_source_pdf(path, sha)


@pytest.mark.anyio
async def test_worker_accounts_for_every_physical_page(tmp_path):
    document = fitz.open()
    document.new_page().insert_text((50, 80), "First legal page with selectable text.")
    document.new_page()
    document.new_page().insert_text((50, 80), "Last legal page with selectable text.")
    data = document.tobytes()
    document.close()
    sha = hashlib.sha256(data).hexdigest()
    target = tmp_path / sha[:2] / f"{sha}.pdf"
    target.parent.mkdir()
    target.write_bytes(data)
    claim = ExtractionClaim(
        job_id=uuid4(), attempt_id=uuid4(), extraction_id=uuid4(), file_id=uuid4(),
        object_key=f"{sha[:2]}/{sha}.pdf", sha256=sha, page_count=3,
    )

    class FakeRepo:
        pages = []
        completed = None
        failed = None

        async def claim_next(self, **kwargs):
            return claim

        async def save_page(self, _claim, page, *, lease_seconds=3600):
            self.pages.append(page)

        async def complete(self, _claim, digest):
            self.completed = digest

        async def fail(self, _claim, message):
            self.failed = message

    repo = FakeRepo()
    assert await process_one(repo, upload_dir=str(tmp_path), config=ExtractionConfig(tessdata_dir="unused"))
    assert [page.page_index for page in repo.pages] == [1, 2, 3]
    assert [page.outcome for page in repo.pages] == ["extracted", "needs_review", "extracted"]
    assert repo.failed is None
    assert len(repo.completed) == 64


@pytest.mark.anyio
async def test_page_error_is_recorded_without_skipping_the_next_page(tmp_path, monkeypatch):
    document = fitz.open()
    document.new_page().insert_text((50, 80), "First page with enough legal source text")
    document.new_page().insert_text((50, 80), "Second page with enough legal source text")
    data = document.tobytes()
    document.close()
    sha = hashlib.sha256(data).hexdigest()
    target = tmp_path / sha[:2] / f"{sha}.pdf"
    target.parent.mkdir()
    target.write_bytes(data)
    claim = ExtractionClaim(
        job_id=uuid4(), attempt_id=uuid4(), extraction_id=uuid4(), file_id=uuid4(),
        object_key=f"{sha[:2]}/{sha}.pdf", sha256=sha, page_count=2,
    )

    class FakeRepo:
        def __init__(self):
            self.pages = []
            self.completed = False

        async def claim_next(self, **kwargs):
            return claim

        async def save_page(self, _claim, page, *, lease_seconds=3600):
            self.pages.append(page)

        async def complete(self, _claim, _digest):
            self.completed = True

        async def fail(self, _claim, message):
            pytest.fail(f"Unexpected whole-job failure: {message}")

    from app.workers import legal_extract

    actual_extract_page = legal_extract.extract_page

    def sometimes_fail(page, index, config):
        if index == 1:
            raise RuntimeError("damaged content stream")
        return actual_extract_page(page, index, config)

    monkeypatch.setattr(legal_extract, "extract_page", sometimes_fail)
    repo = FakeRepo()
    assert await process_one(repo, upload_dir=str(tmp_path), config=ExtractionConfig())
    assert [page.page_index for page in repo.pages] == [1, 2]
    assert [page.outcome for page in repo.pages] == ["failed", "extracted"]
    assert repo.pages[0].warnings[0]["code"] == "page_failed"
    assert repo.completed


def test_table_detection_failure_sends_the_page_to_review(monkeypatch):
    from app.services import legal_extraction

    def broken(page):
        raise RuntimeError("layout engine crashed")
    monkeypatch.setattr(legal_extraction, "_table_regions", broken)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 80), "1. Short title. This Act may be called the Example Act.", fontsize=12)
    result = extract_page(page, 1, ExtractionConfig(tessdata_dir="unused"))
    document.close()
    # An undetected table must not slip through as ordinary extracted text.
    assert result.outcome == "needs_review" and result.table_regions == []
    assert "table_detection_failed" in {item["code"] for item in result.warnings}
