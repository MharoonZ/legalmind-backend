"""Shared test fixtures; never connect to the real development database."""

import os

os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/test"
os.environ["DB_CONNECT_RETRIES"] = "1"
os.environ["DB_CONNECT_TIMEOUT"] = "1"
os.environ["DB_SSL"] = "false"
os.environ["LEGAL_SEARCH_ENABLED"] = "false"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "auto"
os.environ["QA_RECORD_RUNS"] = "false"

from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_document_repository  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.document_repository import DocumentRecord  # noqa: E402


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


class FakeDocumentRepository:
    """Stands in for DocumentRepository without a database."""

    def __init__(self):
        self.records: dict[str, DocumentRecord] = {}
        self.created_calls: list[dict] = []

    async def create_upload(
        self,
        *,
        document_id,
        version_id,
        original_filename,
        content_type,
        pdf_sha256,
        size_bytes,
        page_count,
        storage_path,
        question,
    ) -> DocumentRecord:
        self.created_calls.append({"storage_path": storage_path, "pdf_sha256": pdf_sha256})
        record = DocumentRecord(
            document_id=document_id,
            version_id=version_id,
            version_number=1,
            status="uploaded",
            filename=original_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            page_count=page_count,
            question=question,
            error_message=None,
            created_at=datetime.now(UTC),
        )
        self.records[document_id] = record
        return record

    async def get_latest(self, document_id: str) -> DocumentRecord | None:
        return self.records.get(document_id)


@pytest.fixture
def contracts_api(tmp_path, settings):
    """A TestClient whose /contracts uses a fake, in-memory repository."""
    app = create_app()
    state = {"repository": FakeDocumentRepository()}

    app.dependency_overrides[get_document_repository] = lambda: state["repository"]
    # Real uploads still hit the filesystem (that part isn't mocked); use a
    # throwaway directory instead of the real uploads/contracts folder.
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"upload_dir": str(tmp_path)}
    )

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        test_client.state = state
        yield test_client
