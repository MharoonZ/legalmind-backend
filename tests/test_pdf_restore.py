import hashlib
from pathlib import Path

import pytest

from app.services.legal_extraction import SourcePdfError
from scripts.audit_uploaded_pdfs import restore_missing_blob


def source(tmp_path):
    original = tmp_path / "original.pdf"
    original.write_bytes(b"original source bytes")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    row = {"sha256": digest, "object_key": f"{digest[:2]}/{digest}.pdf",
           "page_count": 1, "byte_size": original.stat().st_size}
    matches = [{"path": str(original), "pages": 1, "bytes": row["byte_size"], "encrypted": False}]
    return original, row, matches


def test_restore_is_idempotent_and_preserves_the_source_bytes(tmp_path):
    original, row, matches = source(tmp_path)
    root = (tmp_path / "application").resolve()
    restored = restore_missing_blob(root, row, matches)
    assert Path(restored).read_bytes() == original.read_bytes()
    assert restore_missing_blob(root, row, matches) is None
    assert original.read_bytes() == b"original source bytes"
    assert not list(root.rglob(".restore-*"))


def test_existing_conflicting_blob_is_rejected_without_overwriting_either_copy(tmp_path):
    original, row, matches = source(tmp_path)
    root = (tmp_path / "application").resolve()
    target = root / row["object_key"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"conflicting data")
    with pytest.raises(SourcePdfError):
        restore_missing_blob(root, row, matches)
    assert target.read_bytes() == b"conflicting data"
    assert original.read_bytes() == b"original source bytes"


def test_registered_key_cannot_escape_the_upload_root(tmp_path):
    _, row, matches = source(tmp_path)
    row["object_key"] = "../outside.pdf"
    with pytest.raises(ValueError):
        restore_missing_blob((tmp_path / "application").resolve(), row, matches)
    assert not (tmp_path / "outside.pdf").exists()
