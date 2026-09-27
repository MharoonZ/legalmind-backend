import os

# Tests must never touch the real database, the real model files, or OpenAI.
# These env vars win over anything in .env.
os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/test"
os.environ["DB_CONNECT_RETRIES"] = "1"
os.environ["DB_CONNECT_TIMEOUT"] = "1"
os.environ["DB_SSL"] = "false"
os.environ["EMBEDDINGS_ENABLED"] = "false"
os.environ["EMBEDDING_WARMUP"] = "false"
os.environ["OPENAI_API_KEY"] = "test-key"

from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_ask_service, get_document_repository, get_law_repository  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.document_repository import DocumentRecord  # noqa: E402
from app.repositories.law_repository import CorpusStats, PassageHit  # noqa: E402
from app.services.ask_service import AskService  # noqa: E402
from app.services.embeddings import EmbeddingService  # noqa: E402
from app.services.llm.base import LLMAnswer  # noqa: E402

SECTION_10_TEXT = (
    "10. What agreements are contracts. All agreements are contracts if they are made "
    "by the free consent of parties competent to contract, for a lawful consideration "
    "and with a lawful object, and are not hereby expressly declared to be void."
)
SECTION_10_ID = "ev:section10"
SECTION_10_QUOTE = (
    "All agreements are contracts if they are made by the free consent of parties "
    "competent to contract"
)


def make_passage(
    evidence_id: str = SECTION_10_ID,
    section_number: int = 10,
    text: str = SECTION_10_TEXT,
    score: float = 0.9,
    retriever: str = "keyword",
) -> PassageHit:
    return PassageHit(
        evidence_id=evidence_id,
        section_id=f"ca1872:test:s{section_number:02d}",
        section_number=section_number,
        section_title="What agreements are contracts",
        page_number=14,
        text=text,
        law_name="Contract Act, 1872",
        jurisdiction="Pakistan",
        source_url="https://pakistancode.gov.pk/",
        source_id="ca1872:test",
        review_status="needs_review",
        score=score,
        retriever=retriever,
    )


class FakeRepository:
    """Stands in for LawRepository without a database."""

    def __init__(self, keyword=None, vector=None, sections=None, error=None):
        self.keyword = keyword if keyword is not None else [make_passage()]
        self.vector = vector or []
        self.sections = sections if sections is not None else [make_passage(retriever="section")]
        self.error = error
        self.calls: list[str] = []

    async def keyword_search(self, question, limit):
        self.calls.append("keyword")
        if self.error:
            raise self.error
        return list(self.keyword)

    async def vector_search(self, embedding, embedding_model, limit):
        self.calls.append("vector")
        if self.error:
            raise self.error
        return list(self.vector)

    async def fetch_sections(self, section_numbers):
        self.calls.append("sections")
        if self.error:
            raise self.error
        return list(self.sections)

    async def stats(self):
        if self.error:
            raise self.error
        return CorpusStats(
            sources=1,
            sections=30,
            passages=41,
            embedded_passages=41,
            embedding_models=("sentence-transformers/all-MiniLM-L6-v2",),
            review_statuses=("needs_review",),
        )


class FakeLLM:
    """Stands in for OpenAIClient; records the prompts it receives."""

    name = "fake"
    model = "fake-model"

    def __init__(self, answer: LLMAnswer | None = None, error: Exception | None = None):
        self.answer = answer or LLMAnswer(
            supported=True,
            answer="An agreement is a contract when the parties consent freely.",
            citations=[{"evidence_id": SECTION_10_ID, "quote": SECTION_10_QUOTE}],
        )
        self.error = error
        self.prompts: list[tuple[str, str]] = []

    @property
    def configured(self) -> bool:
        return True

    async def generate(self, system_prompt: str, user_prompt: str) -> LLMAnswer:
        self.prompts.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return self.answer

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def embeddings():
    return EmbeddingService("test-model", dimension=384, enabled=False)


@pytest.fixture
def build_service(settings, embeddings):
    def _build(repository=None, llm=None) -> AskService:
        return AskService(
            repository or FakeRepository(), embeddings, llm or FakeLLM(), settings
        )

    return _build


@pytest.fixture
def client():
    # Using the client as a context manager runs startup and shutdown (lifespan).
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def api(settings, embeddings):
    """A TestClient whose /ask uses fake retrieval and a fake LLM."""
    app = create_app()
    state = {"repository": FakeRepository(), "llm": FakeLLM()}

    app.dependency_overrides[get_law_repository] = lambda: state["repository"]
    app.dependency_overrides[get_ask_service] = lambda: AskService(
        state["repository"], embeddings, state["llm"], settings
    )

    with TestClient(app) as test_client:
        test_client.state = state  # tests mutate this to change behaviour
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

    with TestClient(app) as test_client:
        test_client.state = state
        yield test_client
