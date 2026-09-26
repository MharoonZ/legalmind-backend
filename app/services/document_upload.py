"""Validate and store an uploaded contract PDF.

This module is the *upload* stage only (Sprint 2, Abdullah's Week 1 scope):
is this a usable PDF, how big is it, how many pages, does it have real
selectable text. It never reads clause boundaries, page-to-clause mapping,
or citations - that is Haroon's extraction stage, run later against the
file this module saves untouched.

The "selectable text" check below opens each page and looks at how much text
comes back, purely as a fast yes/no presence check so scanned PDFs are
rejected up front (Sprint 2 shared rule 7). It does not keep, store, or pass
that text anywhere - real per-clause extraction is a separate, later piece
of work.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from app.core.errors import DocumentStorageError, DocumentValidationError

logger = logging.getLogger(__name__)

# Used to turn a generated id into a filesystem-safe name. Never build the
# on-disk path from the user-supplied filename - that is exactly how a
# path-traversal upload bug gets written.
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class ValidatedPdf:
    """Facts about the file itself, established before anything trusts it."""

    sha256: str
    size_bytes: int
    page_count: int


def _looks_like_pdf(filename: str, content_type: str | None) -> bool:
    if content_type and content_type.lower() == "application/pdf":
        return True
    return filename.lower().endswith(".pdf")


def validate_pdf_bytes(
    data: bytes,
    *,
    filename: str,
    content_type: str | None,
    max_size_bytes: int,
    max_pages: int,
    min_text_chars: int,
) -> ValidatedPdf:
    """Reject anything that is not a small, unencrypted, text-based PDF.

    Raises `DocumentValidationError` with a message that is safe to show
    directly to the person who uploaded the file.
    """
    if not data:
        raise DocumentValidationError("The uploaded file is empty.")
    if not _looks_like_pdf(filename, content_type):
        raise DocumentValidationError("Only PDF files are accepted.")
    if len(data) > max_size_bytes:
        limit_mb = max_size_bytes / (1024 * 1024)
        raise DocumentValidationError(f"File is larger than the {limit_mb:.0f} MB limit.")

    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - PyMuPDF raises its own exception types
        raise DocumentValidationError("File is not a valid or readable PDF.") from exc

    try:
        if pdf.is_encrypted:
            raise DocumentValidationError("Encrypted PDFs are not supported.")

        page_count = pdf.page_count
        if page_count == 0:
            raise DocumentValidationError("PDF has no pages.")
        if page_count > max_pages:
            raise DocumentValidationError(
                f"PDF has {page_count} pages; the limit for this sprint is {max_pages}."
            )

        text_chars = 0
        for page in pdf:
            text_chars += len(page.get_text("text").strip())
            if text_chars >= min_text_chars:
                break
        if text_chars < min_text_chars:
            raise DocumentValidationError(
                "This looks like a scanned PDF with no selectable text. "
                "Scanned/OCR documents are out of scope for this sprint."
            )
    finally:
        pdf.close()

    return ValidatedPdf(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        page_count=page_count,
    )


def new_document_id() -> str:
    return f"doc:{uuid.uuid4().hex}"


def new_version_id() -> str:
    return f"docver:{uuid.uuid4().hex}"


def save_pdf_to_disk(data: bytes, *, upload_dir: str, document_id: str) -> str:
    """Write the original PDF bytes to local disk, named by document id only.

    Returns the path that was written, for storage in `documents.storage_path`.
    """
    safe_id = _SAFE_ID_RE.sub("", document_id)
    directory = Path(upload_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe_id}.pdf"
        path.write_bytes(data)
    except OSError as exc:
        logger.exception("Could not save uploaded PDF to disk")
        raise DocumentStorageError("Could not save the uploaded file.") from exc
    return str(path)
