"""Original legal-PDF storage tests; no database or OCR calls."""

from io import BytesIO

import pytest
from fastapi import UploadFile

from app.core.errors import LegalUploadStorageError, LegalUploadValidationError
from app.services.legal_upload import store_legal_pdf
from tests.test_document_upload import _pdf_bytes


def _upload(data: bytes, filename: str = "Punjab Act.pdf") -> UploadFile:
    return UploadFile(file=BytesIO(data), filename=filename)


@pytest.mark.anyio
async def test_streams_and_stores_pdf_by_hash(tmp_path):
    data = _pdf_bytes(pages=3)
    result = await store_legal_pdf(
        _upload(data), upload_dir=str(tmp_path), max_size_bytes=10_000_000,
        max_pages=100,
    )
    stored = tmp_path / result.object_key
    assert stored.read_bytes() == data
    assert result.byte_size == len(data)
    assert result.page_count == 3
    assert result.object_key == f"{result.sha256[:2]}/{result.sha256}.pdf"
    assert not list(tmp_path.glob(".legal-upload-*"))


@pytest.mark.anyio
async def test_scanned_pdf_is_accepted_for_later_ocr(tmp_path):
    result = await store_legal_pdf(
        _upload(_pdf_bytes(with_text=False)), upload_dir=str(tmp_path),
        max_size_bytes=10_000_000, max_pages=100,
    )
    assert result.page_count == 1


@pytest.mark.anyio
async def test_repeat_upload_reuses_same_blob(tmp_path):
    data = _pdf_bytes()
    first = await store_legal_pdf(_upload(data), upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=100)
    second = await store_legal_pdf(_upload(data), upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=100)
    assert first.object_key == second.object_key
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


@pytest.mark.anyio
async def test_filename_cannot_escape_storage_root(tmp_path):
    result = await store_legal_pdf(
        _upload(_pdf_bytes(), "..\\..\\Punjab Act.pdf"),
        upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=100,
    )
    assert result.original_filename == "Punjab Act.pdf"
    assert (tmp_path / result.object_key).is_file()


@pytest.mark.anyio
@pytest.mark.parametrize("data,filename,error", [
    (b"", "x.pdf", "empty"),
    (b"not a pdf", "x.pdf", "not a PDF"),
    (b"not a pdf", "x.txt", "Choose a PDF"),
    (b"%PDF-broken", "x.pdf", "damaged"),
])
async def test_rejects_invalid_files_without_leaving_temp_files(tmp_path, data, filename, error):
    with pytest.raises(LegalUploadValidationError, match=error):
        await store_legal_pdf(
            _upload(data, filename), upload_dir=str(tmp_path),
            max_size_bytes=10_000_000, max_pages=100,
        )
    assert not list(tmp_path.glob(".legal-upload-*"))


@pytest.mark.anyio
async def test_rejects_encrypted_pdf_and_page_limit(tmp_path):
    with pytest.raises(LegalUploadValidationError, match="Encrypted"):
        await store_legal_pdf(_upload(_pdf_bytes(encrypt=True)), upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=100)
    with pytest.raises(LegalUploadValidationError, match="configured limit"):
        await store_legal_pdf(_upload(_pdf_bytes(pages=3)), upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=2)


@pytest.mark.anyio
async def test_rejects_size_limit_and_bad_existing_blob(tmp_path):
    data = _pdf_bytes()
    with pytest.raises(LegalUploadValidationError, match="upload limit"):
        await store_legal_pdf(_upload(data), upload_dir=str(tmp_path), max_size_bytes=10, max_pages=100)
    assert not list(tmp_path.glob(".legal-upload-*"))

    valid = await store_legal_pdf(_upload(data), upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=100)
    (tmp_path / valid.object_key).write_bytes(b"changed")
    with pytest.raises(LegalUploadStorageError, match="different data"):
        await store_legal_pdf(_upload(data), upload_dir=str(tmp_path), max_size_bytes=10_000_000, max_pages=100)
