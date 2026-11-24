"""Render one page of a registered legal PDF after verifying the stored bytes.

The object key, byte size and SHA-256 recorded at registration are checked
before PyMuPDF opens the file, so a preview can never show a different or
tampered document.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

import fitz

_MAX_PIXELS = 20_000_000


class PdfProblem(Exception):
    """A stable, non-sensitive reason a source PDF could not be shown."""


def resolve_pdf(root: str, object_key: str, sha256: str) -> Path:
    # Registration uses this exact content-addressed format. Neither callers
    # nor a corrupted DB object_key may point at another file or drive.
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise PdfProblem("invalid_pdf_hash")
    if object_key != f"{sha256[:2]}/{sha256}.pdf" or PurePosixPath(object_key).is_absolute():
        raise PdfProblem("unsafe_pdf_location")
    root_path = Path(root).resolve()
    path = (root_path / object_key).resolve()
    if not path.is_relative_to(root_path):
        raise PdfProblem("unsafe_pdf_location")
    if not path.is_file():
        raise PdfProblem("source_pdf_missing")
    return path


def verified_bytes(root: str, *, object_key: str, sha256: str, byte_size: int) -> bytes:
    path = resolve_pdf(root, object_key, sha256)
    try:
        if path.stat().st_size != byte_size:
            raise PdfProblem("source_size_mismatch")
        data = path.read_bytes()
    except OSError as exc:
        raise PdfProblem("source_pdf_unreadable") from exc
    if len(data) != byte_size or hashlib.sha256(data).hexdigest() != sha256:
        raise PdfProblem("source_hash_mismatch")
    return data


def render_page_preview(root: str, *, object_key: str, sha256: str, byte_size: int,
                        page_index: int, dpi: int) -> bytes:
    """PNG of a 1-based page. Blocking: call it from a worker thread."""
    data = verified_bytes(root, object_key=object_key, sha256=sha256, byte_size=byte_size)
    try:
        document = fitz.open(stream=data, filetype="pdf")     # the very bytes that were verified
    except Exception as exc:
        raise PdfProblem("pdf_unreadable") from exc
    with document:
        if document.is_encrypted or document.needs_pass or document.page_count < 1:
            raise PdfProblem("pdf_encrypted_or_empty")
        if page_index < 1 or page_index > document.page_count:
            raise PdfProblem("page_out_of_range")
        page = document[page_index - 1]
        if page.rect.get_area() * (dpi / 72) ** 2 > _MAX_PIXELS:
            raise PdfProblem("preview_page_too_large")
        try:
            return page.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB).tobytes("png")
        except Exception as exc:
            raise PdfProblem("page_processing_failed") from exc
