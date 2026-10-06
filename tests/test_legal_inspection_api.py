"""Read endpoints use fake DB records; preview renders real temporary bytes."""
from uuid import uuid4

import httpx
import pytest

from app.api.routes.legal_inspection import get_inspection_repository
from app.core.config import get_settings
from app.main import create_app
from tests.test_legal_pdf_extraction import native_pdf, store_pdf


class Inspector:
    def __init__(self, source): self.pdf = source
    async def documents(self, **kwargs): return [{"file_id": "test"}]
    async def document(self, id): return None
    async def job(self, id): return {"status": "queued", "attempts": []}
    async def extraction(self, id): return {"status": "running", "coverage_complete": False}
    async def page(self, id, index): return {"page_index": index, "blocks": []}
    async def source(self, id): return dict(self.pdf)


def client(tmp_path, settings, *, host="127.0.0.1", enabled=True, environment="development"):
    with native_pdf() as doc: source = store_pdf(tmp_path, doc.tobytes())
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
        response = await api.get("/local-admin/legal/documents", headers=headers)
    assert response.status_code == status


@pytest.mark.anyio
async def test_original_preview_and_id_based_routes(tmp_path, settings):
    api, source = client(tmp_path, settings)
    prefix = "/local-admin/legal"
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
