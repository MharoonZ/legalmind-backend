"""Approval is local-only and requires a recorded, hash-bound decision."""

from uuid import uuid4

import httpx
import pytest

from app.api.deps import get_database
from app.api.routes import legal_ingestion
from app.core.config import get_settings
from app.main import create_app
from app.repositories.legal_structure_repository import StructureConflict


@pytest.mark.anyio
async def test_local_approval_route_shows_gate_and_records_reason(monkeypatch):
    revision_id = uuid4()
    sha = "a" * 64
    calls = []

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def approval_check(self, requested_id):
            assert requested_id == revision_id
            return {"revision_id": revision_id, "review_state": "draft",
                    "content_sha256": sha, "can_approve": True,
                    "hard_blockers": [], "findings": [{"code": "source_page_needs_review",
                                                  "severity": "error", "message": "Review page."}],
                    "page_review": []}

        async def approve(self, requested_id, *, expected_content_sha256,
                          reason, acknowledge_findings):
            calls.append((requested_id, expected_content_sha256, reason, acknowledge_findings))
            if not acknowledge_findings:
                raise StructureConflict("Acknowledge findings first.")
            return {"revision_id": requested_id, "review_state": "approved",
                    "already_approved": False}

    monkeypatch.setattr(legal_ingestion, "LegalStructureRepository", FakeRepository)
    app = create_app()
    settings = get_settings().model_copy(update={
        "legal_ingest_enabled": True, "environment": "development",
    })
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: object()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 42000))
    root = f"/local-admin/legal/revisions/{revision_id}"
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        check = await client.get(f"{root}/approval-check")
        assert check.status_code == 200
        assert check.json()["findings"][0]["code"] == "source_page_needs_review"
        payload = {"expected_content_sha256": sha,
                   "reason": "Proceed for local Phase B indexing.",
                   "acknowledge_findings": False}
        assert (await client.post(f"{root}/approve", json=payload)).status_code == 409
        payload["acknowledge_findings"] = True
        response = await client.post(f"{root}/approve", json=payload)
        assert response.status_code == 200
        assert response.json()["review_state"] == "approved"
        assert calls[-1] == (revision_id, sha, payload["reason"], True)
        assert (await client.post(f"{root}/approve", json={**payload,
                       "expected_content_sha256": "not-a-sha"})).status_code == 422

    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={
        "environment": "production",
    })
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        assert (await client.get(f"{root}/approval-check")).status_code == 404
        assert (await client.post(f"{root}/approve", json=payload)).status_code == 404
