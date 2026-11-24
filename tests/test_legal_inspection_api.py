"""Read endpoints use fake DB records; preview renders real temporary bytes."""
from uuid import uuid4

import httpx
import pytest

import hashlib

import fitz

from app.api.routes.legal_inspection import get_inspection_repository
from app.core.config import get_settings
from app.main import create_app
from app.services.legal_preview import PdfProblem, render_page_preview, resolve_pdf


def native_pdf():
    document = fitz.open()
    document.new_page().insert_text((72, 72), "1. Short title. This Act may be called the Test Act.")
    return document


def store_pdf(root, data):
    digest = hashlib.sha256(data).hexdigest()
    key = f"{digest[:2]}/{digest}.pdf"
    (root / digest[:2]).mkdir(parents=True, exist_ok=True)
    (root / key).write_bytes(data)
    return {"object_key": key, "sha256": digest, "byte_size": len(data), "page_count": 1}


class Inspector:
    def __init__(self, source): self.pdf = source
    async def documents(self, **kwargs): return [{"file_id": "test"}]
    async def document(self, id): return None
    async def job(self, id): return {"status": "queued", "attempts": []}
    async def extraction(self, id): return {"status": "running", "coverage_complete": False}
    async def page(self, id, index): return {"page_index": index, "blocks": []}
    async def source(self, id): return dict(self.pdf)


def client(tmp_path, settings, *, host="127.0.0.1", enabled=True, environment="development"):
    with native_pdf() as doc:
        source = store_pdf(tmp_path, doc.tobytes())
    app = create_app()
    app.dependency_overrides[get_inspection_repository] = lambda: Inspector(source)
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={
        "legal_ingest_enabled": enabled, "environment": environment, "legal_upload_dir": str(tmp_path)})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 4321)), base_url="http://127.0.0.1:8000"), source


@pytest.mark.anyio
@pytest.mark.parametrize("options,headers,status", [
    ({"enabled": False}, {}, 404), ({"environment": "production"}, {}, 404),
    ({"host": "192.168.1.2"}, {}, 404), ({}, {"Host": "attacker.example"}, 404),
    ({}, {"Origin": "http://attacker.example"}, 403), ({}, {"Origin": "http://localhost:3000"}, 200),
])
async def test_same_local_access_gate_as_upload(tmp_path, settings, options, headers, status):
    api, _ = client(tmp_path, settings, **options)
    async with api:
        response = await api.get("/local-admin/legal/inspection/documents", headers=headers)
    assert response.status_code == status


@pytest.mark.anyio
async def test_original_preview_and_id_based_routes(tmp_path, settings):
    api, source = client(tmp_path, settings)
    prefix = "/local-admin/legal/inspection"
    id = uuid4()
    async with api:
        assert (await api.get(f"{prefix}/documents/{id}")).status_code == 404
        assert (await api.get(f"{prefix}/documents/not-a-uuid")).status_code == 422
        assert (await api.get(f"{prefix}/jobs/{id}")).json()["status"] == "queued"
        assert (await api.get(f"{prefix}/extractions/{id}")).json()["coverage_complete"] is False
        assert (await api.get(f"{prefix}/extractions/{id}/pages/1")).json()["page_index"] == 1
        assert (await api.get(f"{prefix}/extractions/{id}/pages/0")).status_code == 422
        preview = await api.get(f"{prefix}/documents/{id}/pages/1/preview")
        assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
        assert preview.headers["cache-control"] == "no-store"
        assert (await api.get(f"{prefix}/documents/{id}/pages/2/preview")).status_code == 404
        (tmp_path / source["object_key"]).write_bytes(b"tampered")
        response = await api.get(f"{prefix}/documents/{id}/pages/1/preview")
        assert response.status_code == 409 and "source_size_mismatch" in response.text
        assert str(tmp_path) not in response.text


def test_preview_refuses_unsafe_or_altered_sources(tmp_path):
    with native_pdf() as doc:
        source = store_pdf(tmp_path, doc.tobytes())
    arguments = dict(object_key=source["object_key"], sha256=source["sha256"], byte_size=source["byte_size"])
    assert render_page_preview(str(tmp_path), **arguments, page_index=1, dpi=72).startswith(b"\x89PNG")
    for change, code in (({"object_key": "../outside.pdf"}, "unsafe_pdf_location"),
                         ({"sha256": "not-a-hash"}, "invalid_pdf_hash"),
                         ({"byte_size": source["byte_size"] + 1}, "source_size_mismatch")):
        with pytest.raises(PdfProblem, match=code):
            render_page_preview(str(tmp_path), **{**arguments, **change}, page_index=1, dpi=72)
    with pytest.raises(PdfProblem, match="page_out_of_range"):
        render_page_preview(str(tmp_path), **arguments, page_index=5, dpi=72)
    with pytest.raises(PdfProblem, match="preview_page_too_large"):
        render_page_preview(str(tmp_path), **arguments, page_index=1, dpi=100000)
    same_size = bytes(source["byte_size"])
    (tmp_path / source["object_key"]).write_bytes(same_size)
    with pytest.raises(PdfProblem, match="source_hash_mismatch"):
        render_page_preview(str(tmp_path), **arguments, page_index=1, dpi=72)
    missing = "0" * 64
    with pytest.raises(PdfProblem, match="source_pdf_missing"):
        resolve_pdf(str(tmp_path), f"00/{missing}.pdf", missing)
