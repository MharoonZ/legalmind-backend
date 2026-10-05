"""Local-only upload route tests with a fake registry repository."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.api.deps import get_legal_ingestion_repository
from app.core.config import get_settings
from app.main import create_app
from app.repositories.legal_ingestion_repository import LegalUploadRecord
from tests.test_document_upload import _pdf_bytes


class FakeIngestionRepository:
    def __init__(self):
        self.records = []
        self.metadata = None
        self.pdf = None

    async def register_upload(self, metadata, pdf):
        self.metadata, self.pdf = metadata, pdf
        if self.records:
            record = self.records[0]
            return replace(record, already_registered=True)
        record = LegalUploadRecord(
            jurisdiction_id=uuid4(), instrument_id=uuid4(), legal_version_id=uuid4(),
            file_id=uuid4(), job_id=uuid4(), canonical_title=metadata.canonical_title,
            edition_key=metadata.edition_key, original_filename=pdf.original_filename,
            sha256=pdf.sha256, page_count=pdf.page_count, job_status="queued",
            already_registered=False, created_at=datetime.now(UTC),
        )
        self.records.append(record)
        return record

    async def recent_uploads(self, *, limit=25):
        return self.records[:limit]


def _form():
    return {
        "jurisdiction_code": "PK-PB",
        "jurisdiction_name": "Punjab, Pakistan",
        "category_slug": "criminal-law",
        "category_name": "Criminal Law",
        "instrument_type": "act",
        "canonical_title": "Test Criminal Act",
        "citation_key": "test-criminal-act",
        "edition_key": "official-pdf-test",
        "assignment_role": "primary",
    }


async def _client(tmp_path, *, enabled=True, environment="development", client_host="127.0.0.1"):
    app = create_app()
    repo = FakeIngestionRepository()
    settings = get_settings().model_copy(update={
        "legal_ingest_enabled": enabled, "environment": environment,
        "legal_upload_dir": str(tmp_path), "legal_upload_max_size_bytes": 10_000_000,
        "legal_upload_max_pages": 100,
    })
    app.dependency_overrides[get_legal_ingestion_repository] = lambda: repo
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app, client=(client_host, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000"), repo


@pytest.mark.anyio
async def test_upload_registers_file_edition_and_job(tmp_path):
    client, repo = await _client(tmp_path)
    async with client:
        response = await client.post(
            "/local-admin/legal/uploads", data=_form(),
            files={"file": ("criminal.pdf", _pdf_bytes(pages=2), "application/pdf")},
            headers={"Origin": "http://localhost:3000"},
        )
        assert response.status_code == 201
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        body = response.json()
        assert body["page_count"] == 2
        assert body["job_status"] == "queued"
        assert body["already_registered"] is False
        assert repo.metadata.category_slug == "criminal-law"
        assert repo.metadata.jurisdiction_code == "PK-PB"
        assert (tmp_path / repo.pdf.object_key).is_file()

        listed = await client.get("/local-admin/legal/uploads")
        assert listed.status_code == 200
        assert len(listed.json()) == 1


@pytest.mark.anyio
async def test_duplicate_upload_returns_same_job(tmp_path):
    client, _ = await _client(tmp_path)
    async with client:
        kwargs = {"data": _form(), "files": {"file": ("criminal.pdf", _pdf_bytes(), "application/pdf")}}
        first = await client.post("/local-admin/legal/uploads", **kwargs)
        second = await client.post("/local-admin/legal/uploads", **kwargs)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]


@pytest.mark.anyio
async def test_scanned_pdf_is_registered_for_future_ocr(tmp_path):
    client, _ = await _client(tmp_path)
    async with client:
        response = await client.post(
            "/local-admin/legal/uploads", data=_form(),
            files={"file": ("scan.pdf", _pdf_bytes(with_text=False), "application/pdf")},
        )
    assert response.status_code == 201


@pytest.mark.anyio
@pytest.mark.parametrize("enabled,environment,client_host", [
    (False, "development", "127.0.0.1"),
    (True, "staging", "127.0.0.1"),
    (True, "production", "127.0.0.1"),
    (True, "development", "203.0.113.8"),
])
async def test_upload_is_not_available_outside_local_development(tmp_path, enabled, environment, client_host):
    client, repo = await _client(tmp_path, enabled=enabled, environment=environment, client_host=client_host)
    async with client:
        response = await client.post(
            "/local-admin/legal/uploads", data=_form(),
            files={"file": ("criminal.pdf", _pdf_bytes(), "application/pdf")},
        )
    assert response.status_code == 404
    assert repo.records == []


@pytest.mark.anyio
async def test_upload_rejects_untrusted_origin(tmp_path):
    client, repo = await _client(tmp_path)
    async with client:
        response = await client.post(
            "/local-admin/legal/uploads", data=_form(),
            files={"file": ("criminal.pdf", _pdf_bytes(), "application/pdf")},
            headers={"Origin": "https://other.example"},
        )
    assert response.status_code == 403
    assert repo.records == []


@pytest.mark.anyio
async def test_upload_rejects_nonlocal_host_header(tmp_path):
    client, repo = await _client(tmp_path)
    async with client:
        response = await client.post(
            "/local-admin/legal/uploads", data=_form(),
            files={"file": ("criminal.pdf", _pdf_bytes(), "application/pdf")},
            headers={"Host": "public.example"},
        )
    assert response.status_code == 404
    assert repo.records == []


@pytest.mark.anyio
async def test_invalid_registration_is_rejected_before_storage(tmp_path):
    client, repo = await _client(tmp_path)
    form = {**_form(), "citation_key": "Bad Key!"}
    async with client:
        response = await client.post(
            "/local-admin/legal/uploads", data=form,
            files={"file": ("criminal.pdf", _pdf_bytes(), "application/pdf")},
        )
    assert response.status_code == 422
    assert repo.records == []
    assert not list(tmp_path.rglob("*.pdf"))
