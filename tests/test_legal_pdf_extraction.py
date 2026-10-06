"""Synthetic PDF fixtures, not results for the real six-page pilot."""
import base64
import asyncio
import hashlib
import sys

import fitz
import pytest

from app.services import legal_pdf_extraction as pdf
from app.services.pdf_process import pdf_process


@pytest.fixture
def extraction_config(settings):
    return pdf.extraction_config(settings.model_copy(update={"legal_tesseract_path": "missing-tesseract-test.exe"}))


def store_pdf(tmp_path, data):
    digest = hashlib.sha256(data).hexdigest()
    key = f"{digest[:2]}/{digest}.pdf"
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"root": str(tmp_path), "object_key": key, "sha256": digest, "byte_size": len(data), "page_count": 1}


def native_pdf(rotation=0):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((35, 50), "Synthetic fixture: preserve every native word, number 19A, and punctuation.", fontsize=10)
    page.insert_text((35, 100), "Tiny footnote is also retained.", fontsize=5)
    page.set_rotation(rotation)
    return doc


def scanned_pdf(mixed=False):
    original = native_pdf()
    image = original[0].get_pixmap().tobytes("png")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 150, 595, 842), stream=image)
    if mixed:
        page.insert_text((35, 40), "A native heading with enough text to pass the sparse threshold.")
    original.close()
    return doc


def test_normalization_changes_only_line_endings_and_trailing_space():
    assert pdf.normalize_text("  Section 19A.  \r\nshall NOT—change!\t\rnext\n") == "  Section 19A.\nshall NOT—change!\nnext\n"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_native_all_fonts_and_offsets(extraction_config, rotation):
    with native_pdf(rotation) as doc:
        result = pdf.page_result(doc[0], extraction_config)
        assert result["extraction_method"] == "native"
        assert result["outcome"] == "extracted"
        assert "Tiny footnote" in result["normalized_text"]
        assert result["rotation_deg"] == rotation
        assert result["width_pt"] == doc[0].rect.width
        assert result["ocr_confidence"] is None
        coords = [(b["bbox"]["y0"], b["bbox"]["x0"]) for b in result["blocks"]]
        assert coords == sorted(coords)
        for block in result["blocks"]:
            assert result["normalized_text"][block["page_char_start"]:block["page_char_end"]] == block["normalized_text"]
            assert block["bbox"]["space"] == "rotated_page_points"


@pytest.mark.parametrize("kind", ["empty", "scanned", "mixed"])
def test_missing_ocr_is_explicit_never_verified_blank(extraction_config, kind):
    doc = fitz.open() if kind == "empty" else scanned_pdf(kind == "mixed")
    if kind == "empty":
        doc.new_page()
    with doc:
        result = pdf.page_result(doc[0], extraction_config)
    assert result["outcome"] == ("needs_review" if kind == "mixed" else "failed")
    assert any(w["code"] == "tesseract_missing" for w in result["warnings"])
    assert result["ocr_confidence"] is None
    assert result["extraction_method"] == ("native" if kind == "mixed" else "none")


@pytest.mark.parametrize("mixed", [False, True])
def test_mocked_ocr_scanned_and_mixed(monkeypatch, extraction_config, mixed):
    # Only OCR is mocked. PDF text/image detection and ordered output are real.
    def recognize(page, config, native_boxes):
        assert bool(native_boxes) == mixed
        return [{"raw_text": "Synthetic OCR transcription.", "block_kind": "other", "warnings": [],
                 "bbox": {"x0": 40, "y0": 200, "x1": 250, "y1": 220, "space": "rotated_page_points"}}]
    monkeypatch.setattr(pdf, "recognize", recognize)
    with scanned_pdf(mixed) as doc:
        result = pdf.page_result(doc[0], extraction_config)
    assert result["extraction_method"] == ("mixed" if mixed else "ocr")
    assert result["outcome"] == "needs_review"
    assert "Synthetic OCR" in result["normalized_text"]
    if mixed:
        assert "native heading" in result["normalized_text"]


def test_empty_ocr_output_does_not_prove_blank(monkeypatch, extraction_config):
    monkeypatch.setattr(pdf, "recognize", lambda *args: [])
    with fitz.open() as doc:
        doc.new_page()
        result = pdf.page_result(doc[0], extraction_config)
    assert result["outcome"] == "needs_review"
    assert result["extraction_method"] == "none"
    assert "no_text_unverified" in {w["code"] for w in result["warnings"]}


def test_corrupt_native_text_routes_to_ocr(monkeypatch, extraction_config):
    class CorruptPage:
        rect = fitz.Rect(0, 0, 600, 800)
        rotation_matrix = fitz.Matrix(1, 1)
        rotation = number = 0
        def get_label(self): return ""
        def get_text(self, *args, **kwargs): return [(10, 10, 500, 30, "Broken \ufffd\ufffd\ufffd text", 0, 0)]
    calls = []
    monkeypatch.setattr(pdf, "recognize", lambda *args: calls.append(args) or [])
    result = pdf.page_result(CorruptPage(), extraction_config)
    assert calls and "corrupt_native_text" in {w["code"] for w in result["warnings"]}
    assert result["outcome"] == "needs_review"


def test_tsv_excludes_native_overlap_and_rejects_nonfinite_boxes():
    header = "level\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    words = "5\t1\t1\t1\t10\t10\t20\t10\t92\tExisting\n5\t1\t1\t2\t10\t80\t50\t10\t87\tScanned\n"
    blocks = pdf.tsv_blocks(header + words, 1, 1, [(0, 0, 60, 30)])
    assert [b["raw_text"] for b in blocks] == ["Scanned"]
    quoted = pdf.tsv_blocks(header + words.replace("Scanned", '"Scanned"'), 1, 1, [(0, 0, 60, 30)])
    assert quoted[0]["raw_text"] == '"Scanned"'
    with pytest.raises(pdf.PdfProblem, match="ocr_invalid_output"):
        pdf.tsv_blocks(header + words.replace("\t80\t", "\tnan\t"), 1, 1)


def test_integrity_path_safety_and_corrupt_pdf(tmp_path):
    source = store_pdf(tmp_path, b"not a PDF")
    with pytest.raises(pdf.PdfProblem, match="pdf_unreadable"):
        pdf.open_pdf(source)
    for key in ("../secret.env", "C:/secret.env", "37/wrong.pdf"):
        with pytest.raises(pdf.PdfProblem, match="unsafe_pdf_location"):
            pdf.resolve_pdf(source["root"], key, source["sha256"])
    (tmp_path / source["object_key"]).write_bytes(b"NOT a PDF")
    with pytest.raises(pdf.PdfProblem, match="source_hash_mismatch"):
        pdf.verified_bytes(source)
    with pytest.raises(pdf.PdfProblem, match="source_size_mismatch"):
        pdf.verified_bytes({**source, "byte_size": 100})


@pytest.mark.anyio
async def test_child_process_extracts_and_renders_original(tmp_path, extraction_config):
    with native_pdf() as doc:
        source = store_pdf(tmp_path, doc.tobytes())
    info = await pdf_process({"operation": "inspect", "source": source}, 20)
    assert info["page_count"] == 1 and info["engine_version"] == "1.26.3"
    result = await pdf_process({"operation": "page", "source": source, "page_index": 1, "config": extraction_config}, 20)
    assert "Tiny footnote" in result["normalized_text"]
    image = await pdf_process({"operation": "preview", "source": source, "page_index": 1, "dpi": 72}, 20)
    assert base64.b64decode(image["png"]).startswith(b"\x89PNG")
    with pytest.raises(pdf.PdfProblem, match="page_out_of_range"):
        await pdf_process({"operation": "preview", "source": source, "page_index": 2, "dpi": 72}, 20)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Uvicorn reload regression")
def test_preview_works_on_windows_selector_loop(tmp_path):
    with native_pdf() as doc: source = store_pdf(tmp_path, doc.tobytes())
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        result = runner.run(pdf_process({"operation": "preview", "source": source, "page_index": 1, "dpi": 72}, 20))
    assert base64.b64decode(result["png"]).startswith(b"\x89PNG")
