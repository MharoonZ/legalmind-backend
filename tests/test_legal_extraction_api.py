"""Local inspector API serves registered pages, never arbitrary disk paths."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import fitz
import httpx
import pytest

from app.api.deps import get_database
from app.api.routes import legal_ingestion
from app.core.config import get_settings
from app.main import create_app


@pytest.mark.anyio
async def test_inspector_overview_and_pdf_preview_are_local_only(tmp_path, monkeypatch):
    pdf = fitz.open()
    pdf.new_page().insert_text((50, 80), "Visible source text")
    data = pdf.tobytes()
    pdf.close()
    sha = hashlib.sha256(data).hexdigest()
    target = tmp_path / sha[:2] / f"{sha}.pdf"
    target.parent.mkdir()
    target.write_bytes(data)
    file_id = uuid4()
    extraction_id = uuid4()

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def get_file(self, requested_id):
            if requested_id != file_id:
                return None
            return SimpleNamespace(
                id=file_id, object_key=f"{sha[:2]}/{sha}.pdf", sha256=sha, page_count=1
            )

        async def overview(self, requested_id):
            if requested_id != file_id:
                return None
            return {
                "file_id": file_id, "title": "Test Act", "edition_key": "first",
                "original_filename": "act.pdf", "page_count": 1, "source_url": None,
                "job": {"id": uuid4(), "status": "completed", "progress_done": 1,
                        "progress_total": 1, "error_message": None},
                "extractions": [{
                    "id": extraction_id, "status": "completed", "engine_name": "pymupdf",
                    "engine_version": "1.26.3", "ocr_languages": "eng", "config_sha256": sha,
                    "output_sha256": sha, "page_count": 1, "outcomes": {"extracted": 1},
                    "created_at": datetime.now(UTC),
                }],
            }

    monkeypatch.setattr(legal_ingestion, "LegalExtractionRepository", FakeRepository)
    app = create_app()
    settings = get_settings().model_copy(update={
        "legal_ingest_enabled": True, "environment": "development",
        "legal_upload_dir": str(tmp_path),
    })
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: object()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 42000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        overview = await client.get(f"/local-admin/legal/files/{file_id}/overview")
        assert overview.status_code == 200
        assert overview.json()["extractions"][0]["outcomes"] == {"extracted": 1}
        preview = await client.get(f"/local-admin/legal/files/{file_id}/pages/1/preview")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert preview.content.startswith(b"\x89PNG")
        assert (await client.get(f"/local-admin/legal/files/{file_id}/pages/2/preview")).status_code == 404
        assert (await client.get(f"/local-admin/legal/files/{uuid4()}/pages/1/preview")).status_code == 404

    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"environment": "production"})
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        assert (await client.get(f"/local-admin/legal/files/{file_id}/overview")).status_code == 404
