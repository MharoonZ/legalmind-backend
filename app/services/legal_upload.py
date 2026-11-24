"""Validate and persist original Act/Rules PDFs for local corpus ingestion.

This stage intentionally does not extract text. Scanned PDFs are accepted so
the later page worker can route them through OCR. Storage is content-addressed
and never uses an untrusted filename as a path component.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import fitz
from fastapi import UploadFile

from app.core.errors import LegalUploadStorageError, LegalUploadValidationError

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredLegalPdf:
    original_filename: str
    object_key: str
    sha256: str
    byte_size: int
    page_count: int


def safe_display_name(filename: str | None) -> str:
    name = PureWindowsPath(filename or "upload.pdf").name
    name = Path(name).name
    name = "".join(char for char in name if char.isprintable()).strip()
    if not name or name in {".", ".."}:
        name = "upload.pdf"
    return name[:255]


def _verify_existing_blob(path: Path, expected_hash: str, expected_size: int) -> None:
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            raise LegalUploadStorageError("Stored PDF hash path is occupied by different data.")
        digest = hashlib.sha256()
        with path.open("rb") as existing:
            for chunk in iter(lambda: existing.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise LegalUploadStorageError("Stored PDF hash path is occupied by different data.")
    except OSError as exc:
        raise LegalUploadStorageError("Could not verify the stored PDF.") from exc


async def store_legal_pdf(
    upload: UploadFile,
    *,
    upload_dir: str,
    max_size_bytes: int,
    max_pages: int,
) -> StoredLegalPdf:
    """Stream, validate, hash and atomically store one PDF by its digest."""
    if max_size_bytes <= 0 or max_pages <= 0:
        raise ValueError("Legal upload limits must be positive.")

    original_filename = safe_display_name(upload.filename)
    if not original_filename.lower().endswith(".pdf"):
        raise LegalUploadValidationError("Choose a PDF file with a .pdf filename.")

    root = Path(upload_dir).resolve()
    temporary_path: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        byte_size = 0
        header = b""
        with tempfile.NamedTemporaryFile(dir=root, prefix=".legal-upload-", suffix=".pdf", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            while chunk := await upload.read(_CHUNK_SIZE):
                byte_size += len(chunk)
                if byte_size > max_size_bytes:
                    raise LegalUploadValidationError(
                        f"PDF exceeds the {max_size_bytes // (1024 * 1024)} MB upload limit."
                    )
                if len(header) < 5:
                    header += chunk[: 5 - len(header)]
                digest.update(chunk)
                temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())

        if byte_size == 0:
            raise LegalUploadValidationError("The uploaded file is empty.")
        if header != b"%PDF-":
            raise LegalUploadValidationError("The file is not a PDF.")

        try:
            # PyMuPDF can retain a Windows file handle when opening a damaged
            # path raises. A bounded in-memory validation copy lets us always
            # remove the temporary file after a corrupt upload.
            pdf = fitz.open(stream=temporary_path.read_bytes(), filetype="pdf")
        except Exception as exc:  # PyMuPDF uses several error subclasses.
            raise LegalUploadValidationError("The PDF is damaged or unreadable.") from exc
        try:
            if pdf.is_encrypted or pdf.needs_pass:
                raise LegalUploadValidationError("Encrypted PDFs cannot be ingested.")
            page_count = pdf.page_count
            if page_count == 0:
                raise LegalUploadValidationError("The PDF has no pages.")
            if page_count > max_pages:
                raise LegalUploadValidationError(
                    f"The PDF has {page_count} pages; the configured limit is {max_pages}."
                )
        finally:
            pdf.close()

        sha256 = digest.hexdigest()
        object_key = f"{sha256[:2]}/{sha256}.pdf"
        destination = root / object_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # A hard link publishes a fully written file without replacing a
            # blob another upload may already have stored under this hash.
            os.link(temporary_path, destination)
        except FileExistsError:
            _verify_existing_blob(destination, sha256, byte_size)
        return StoredLegalPdf(
            original_filename=original_filename,
            object_key=object_key,
            sha256=sha256,
            byte_size=byte_size,
            page_count=page_count,
        )
    except OSError as exc:
        raise LegalUploadStorageError("Could not store the legal PDF.") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
