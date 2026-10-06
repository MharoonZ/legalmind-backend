"""Draft structure endpoints stay local-only and never approve a revision."""

from uuid import uuid4

import httpx
import pytest

from app.api.deps import get_database
from app.api.routes import legal_ingestion
from app.core.config import get_settings
from app.main import create_app


@pytest.mark.anyio
async def test_draft_routes_and_provenance_are_loopback_only(monkeypatch):
    version_id, revision_id, provision_id, file_id, extraction_id = (uuid4() for _ in range(5))

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def source_options(self, requested):
            assert requested == version_id
            return [{"file_id": file_id, "filename": "act.pdf", "page_count": 2,
                     "assignment_role": "primary", "extraction_id": extraction_id,
                     "extraction_status": "completed"}]

        async def create_revision(self, requested, selected):
            assert requested == version_id
            assert selected == [extraction_id]
            return {"revision_id": revision_id, "revision_number": 1,
                    "job_id": uuid4(), "job_status": "queued", "source_count": 1,
                    "source_warnings": []}

        async def list_revisions(self, requested):
            assert requested == version_id
            return [{"id": revision_id, "revision_number": 1, "review_state": "draft",
                     "job_status": "queued", "provision_count": None}]

        async def overview(self, requested):
            assert requested == revision_id
            return {"id": revision_id, "review_state": "draft", "provisions": []}

        async def provision_detail(self, requested_revision, requested_provision):
            assert requested_revision == revision_id
            assert requested_provision == provision_id
            return {"id": provision_id, "own_text": "Exact legal text", "spans": [
                {"file_id": file_id, "page_index": 2, "block_id": uuid4(),
                 "mapping_quality": "exact"}]}

        async def coverage(self, requested, **kwargs):
            assert requested == revision_id
            assert kwargs["status"] == "unmapped"
            return []

    monkeypatch.setattr(legal_ingestion, "LegalStructureRepository", FakeRepository)
    app = create_app()
    settings = get_settings().model_copy(update={"legal_ingest_enabled": True,
                                                "environment": "development"})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: object()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 42000))
    base = "/local-admin/legal"
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        sources = await client.get(f"{base}/versions/{version_id}/structure-sources")
        assert sources.status_code == 200
        assert sources.json()[0]["extraction_id"] == str(extraction_id)
        assert (await client.get(f"{base}/versions/{version_id}/revisions")).json()[0]["id"] == str(revision_id)
        created = await client.post(f"{base}/versions/{version_id}/revisions",
                                    json={"extraction_ids": [str(extraction_id)]})
        assert created.status_code == 201
        assert created.json()["job_status"] == "queued"
        overview = await client.get(f"{base}/revisions/{revision_id}")
        assert overview.json()["review_state"] == "draft"
        detail = await client.get(f"{base}/revisions/{revision_id}/provisions/{provision_id}")
        assert detail.json()["spans"][0]["page_index"] == 2
        assert (await client.get(f"{base}/revisions/{revision_id}/coverage?status=unmapped")).json() == []
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"environment": "production"})
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        assert (await client.get(f"{base}/revisions/{revision_id}")).status_code == 404
