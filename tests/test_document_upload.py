"""Unit tests for app.services.document_upload - no database involved.

PDFs are built on the fly with PyMuPDF so the tests don't depend on any
fixture files.
"""

import fitz
import pytest

from app.core.errors import DocumentValidationError
from app.services.document_upload import (
    new_document_id,
    new_version_id,
    save_pdf_to_disk,
    validate_pdf_bytes,
)

DEFAULTS = {
    "max_size_bytes": 10 * 1024 * 1024,
    "max_pages": 10,
    "min_text_chars": 20,
}


def _pdf_bytes(*, pages: int = 1, with_text: bool = True, encrypt: bool = False) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        if with_text:
            page.insert_text((72, 72), "This is a service agreement between two parties.")
    kwargs = {}
    if encrypt:
        kwargs = {
            "encryption": fitz.PDF_ENCRYPT_AES_256,
            "owner_pw": "owner-secret",
            "user_pw": "user-secret",
        }
    data = doc.tobytes(**kwargs)
    doc.close()
    return data


def test_valid_pdf_passes():
    data = _pdf_bytes(pages=2)
    result = validate_pdf_bytes(
        data, filename="contract.pdf", content_type="application/pdf", **DEFAULTS
    )
    assert result.page_count == 2
    assert result.size_bytes == len(data)
    assert len(result.sha256) == 64


def test_empty_file_is_rejected():
    with pytest.raises(DocumentValidationError, match="empty"):
        validate_pdf_bytes(b"", filename="contract.pdf", content_type="application/pdf", **DEFAULTS)


def test_non_pdf_content_type_and_extension_is_rejected():
    with pytest.raises(DocumentValidationError, match="Only PDF"):
        validate_pdf_bytes(
            b"not a pdf", filename="contract.docx", content_type="text/plain", **DEFAULTS
        )


def test_oversized_file_is_rejected():
    data = _pdf_bytes(pages=1)
    with pytest.raises(DocumentValidationError, match="MB limit"):
        validate_pdf_bytes(
            data,
            filename="contract.pdf",
            content_type="application/pdf",
            max_size_bytes=10,  # smaller than any real PDF
            max_pages=10,
            min_text_chars=20,
        )


def test_too_many_pages_is_rejected():
    data = _pdf_bytes(pages=12)
    with pytest.raises(DocumentValidationError, match="12 pages"):
        validate_pdf_bytes(
            data, filename="contract.pdf", content_type="application/pdf", **DEFAULTS
        )


def test_encrypted_pdf_is_rejected():
    data = _pdf_bytes(pages=1, encrypt=True)
    with pytest.raises(DocumentValidationError, match="Encrypted"):
        validate_pdf_bytes(
            data, filename="contract.pdf", content_type="application/pdf", **DEFAULTS
        )


def test_pdf_with_no_selectable_text_is_rejected():
    data = _pdf_bytes(pages=1, with_text=False)
    with pytest.raises(DocumentValidationError, match="scanned"):
        validate_pdf_bytes(
            data, filename="contract.pdf", content_type="application/pdf", **DEFAULTS
        )


def test_corrupt_pdf_bytes_are_rejected():
    with pytest.raises(DocumentValidationError, match="not a valid or readable PDF"):
        validate_pdf_bytes(
            b"%PDF-1.4 this is garbage, not a real pdf structure",
            filename="contract.pdf",
            content_type="application/pdf",
            **DEFAULTS,
        )


def test_ids_are_unique_and_prefixed():
    doc_id_a, doc_id_b = new_document_id(), new_document_id()
    ver_id_a, ver_id_b = new_version_id(), new_version_id()
    assert doc_id_a != doc_id_b
    assert ver_id_a != ver_id_b
    assert doc_id_a.startswith("doc:")
    assert ver_id_a.startswith("docver:")


def test_save_pdf_to_disk_writes_bytes_and_ignores_unsafe_id(tmp_path):
    data = _pdf_bytes(pages=1)
    unsafe_id = "doc:../../etc/passwd"
    path = save_pdf_to_disk(data, upload_dir=str(tmp_path), document_id=unsafe_id)

    written = tmp_path.__class__(path)
    assert written.exists()
    assert written.read_bytes() == data
    # The path traversal characters must not survive into the filename.
    assert ".." not in written.name
    assert "/" not in written.name
