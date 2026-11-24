"""HTTP contracts of /ask, /search/legal and the health probes."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import deps
from app.api.deps import get_services
from app.bootstrap import AppServices
from app.core.config import get_settings
from app.core.inference import InferenceRuntime
from app.core.ratelimit import RateLimiter
from app.main import create_app
from app.qa.llm import GenerationUnavailable
from app.qa.rerank import CrossEncoderReranker
from app.qa.service import QuestionAnswerService
from app.retrieval.index import PublicationIndex
from app.retrieval.service import QueryEncoder, RetrievalService
from tests.corpus import GAMBLING, fake_encode, make_snapshot
from tests.corpus import settings as pipeline_settings
from tests.test_qa_service import SECTION_3, ScriptedModel

QUESTION = {"question": "What is Section 3 of Punjb Prevention of Gambling Ordinance 1978?"}


def services(model, *, snapshot=None, slots=4) -> AppServices:
    config = pipeline_settings()
    index = PublicationIndex.from_snapshot(make_snapshot() if snapshot is None else snapshot or None)
    retrieval = RetrievalService(index, config, encoder=QueryEncoder(runtime=InferenceRuntime(), encode=fake_encode))
    reranker = CrossEncoderReranker(config)
    return AppServices(index, retrieval, reranker, model,
                       QuestionAnswerService(retrieval, reranker, model, config), asyncio.Semaphore(slots))


@pytest.fixture(autouse=True)
def fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(deps, "_limiter", RateLimiter())


@pytest.fixture
def api(settings):
    """A client whose search is enabled and backed by the in-memory test publication."""
    app = create_app()
    state = {"settings": settings.model_copy(update={"legal_search_enabled": True}),
             "services": services(ScriptedModel(SECTION_3))}
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    app.dependency_overrides[get_services] = lambda: state["services"]
    with TestClient(app) as client:
        client.state = state
        yield client


def test_ask_and_search_are_hidden_until_search_is_enabled(client):
    assert client.post("/ask", json=QUESTION).status_code == 404
    assert client.post("/search/legal", json={"query": "receipt"}).status_code == 404


def test_ask_reports_starting_when_services_are_not_ready(client, settings):
    client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"legal_search_enabled": True})
    response = client.post("/ask", json=QUESTION)
    assert response.status_code == 503 and "not ready" in response.json()["detail"]


def test_ask_answers_the_reported_misspelt_question(api):
    response = api.post("/ask", json=QUESTION, headers={"X-Request-ID": "trace-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered" and body["question"] == QUESTION["question"]
    assert body["answer"].endswith("[1].") and body["citations"][0]["law"] == GAMBLING
    assert body["citations"][0]["label"] == "3" and body["claims"][0]["citation_indexes"] == [0]
    assert set(body) == {"status", "question", "answer", "publication_id", "claims", "citations",
                         "clarification_question", "clarification_options", "limitations", "missing_support"}
    assert response.headers["x-request-id"] == "trace-123"
    assert "total;dur=" in response.headers["server-timing"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unsafe_request_ids_are_replaced(api):
    response = api.post("/ask", json={"question": "hello"}, headers={"X-Request-ID": "bad id\twith spaces"})
    assert response.status_code == 200 and response.headers["x-request-id"] != "bad id\twith spaces"
    assert len(response.headers["x-request-id"]) == 16


def test_provider_failure_is_a_503_with_the_answer_shape(api):
    api.state["services"] = services(ScriptedModel(GenerationUnavailable("down")))
    response = api.post("/ask", json={"question": "what is the punishment for public gaming?"})
    assert response.status_code == 503 and response.json()["status"] == "service_unavailable"
    assert response.json()["citations"] == []
    # A named provision can still be quoted without the model: that is a normal reply.
    api.state["services"] = services(ScriptedModel(GenerationUnavailable("down")))
    quoted = api.post("/ask", json=QUESTION)
    assert quoted.status_code == 200 and quoted.json()["status"] == "partial"
    assert quoted.json()["citations"]


@pytest.mark.parametrize("payload", [
    {"question": ""}, {"question": "   "}, {"question": "??"}, {"question": "x" * 4001}, {},
    {"question": "ok?", "unknown": 1}, {"question": "ok?", "law": ""},
    {"question": "ok?", "history": [{"role": "system", "content": "x"}]},
    {"question": "ok?", "history": [{"role": "user", "content": ""}]},
    {"question": "ok?", "context": {"previous_query": "x" * 4001}},
])
def test_invalid_questions_are_input_errors(api, payload):
    assert api.post("/ask", json=payload).status_code == 422


def test_long_conversations_are_accepted_and_trimmed_by_the_server(api):
    history = [{"role": "user" if index % 2 == 0 else "assistant", "content": "earlier text " * 50}
               for index in range(40)]
    assert api.post("/ask", json={**QUESTION, "history": history}).status_code == 200


def test_a_slow_answer_times_out_as_503(api, settings):
    class Slow(ScriptedModel):
        async def generate_json(self, *args, **kwargs):
            await asyncio.sleep(5)
    api.state["services"] = services(Slow())
    api.state["settings"] = api.state["settings"].model_copy(update={"qa_request_timeout_seconds": 0.05})
    response = api.post("/ask", json=QUESTION)
    assert response.status_code == 503 and "timed out" in response.json()["missing_support"]


def test_a_full_service_asks_the_client_to_retry(api):
    api.state["services"] = services(ScriptedModel(SECTION_3), slots=1)
    api.state["settings"] = api.state["settings"].model_copy(update={"qa_queue_timeout_seconds": 0.05})
    asyncio.run(api.state["services"].ask_slots.acquire())          # every slot is taken
    response = api.post("/ask", json=QUESTION)
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert "busy" in response.json()["missing_support"]


def test_api_keys_protect_the_public_endpoints_when_configured(api):
    api.state["settings"] = api.state["settings"].model_copy(
        update={"api_keys": SecretStr("first-key, second-key")})
    assert api.post("/ask", json={"question": "hello"}).status_code == 401
    assert api.post("/ask", json={"question": "hello"}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert api.post("/search/legal", json={"query": "receipt"}).status_code == 401
    assert api.post("/ask", json={"question": "hello"}, headers={"X-API-Key": "second-key"}).status_code == 200
    assert api.post("/ask", json={"question": "hello"},
                    headers={"Authorization": "Bearer first-key"}).status_code == 200
    assert api.get("/health").status_code == 200           # probes stay open


def test_rate_limit_returns_429_with_retry_after(api):
    api.state["settings"] = api.state["settings"].model_copy(update={"rate_limit_per_minute": 2})
    codes = [api.post("/ask", json={"question": "hello"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = api.post("/search/legal", json={"query": "receipt"})
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_search_returns_ranked_candidates_and_validates_input(api):
    response = api.post("/search/legal", json={"query": "penalty for gamng in a publc place", "limit": 3})
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok" and len(body["results"]) == 3
    top = body["results"][0]
    assert top["instrument_title"] == GAMBLING and top["label"] == "5" and top["rank"] == 1
    assert top["source_refs"][0]["page_index"] >= 1 and top["chunk_text"].startswith(GAMBLING)
    exact = api.post("/search/legal", json={"query": "Section 3 of the Punjb Prevention of Gambling Ordinance"})
    assert exact.json()["results"][0]["exact_rank"] == 1
    for payload in ({"query": ""}, {"query": "receipt", "limit": 999},
                    {"query": "receipt", "revision_ids": ["unpublished"]},
                    {"query": "receipt", "context": {"previous_query": "x" * 4001}}):
        assert api.post("/search/legal", json=payload).status_code == 422


def test_search_timeout_and_missing_publication_are_503(api):
    api.state["services"] = services(ScriptedModel(), snapshot=False)
    missing = api.post("/search/legal", json={"query": "receipt"})
    assert missing.status_code == 200 and missing.json()["status"] == "no_active_publication"

    class Stalled:
        async def search(self, payload):
            await asyncio.sleep(5)
    stalled = services(ScriptedModel())
    stalled.retrieval = Stalled()
    api.state["services"] = stalled
    api.state["settings"] = api.state["settings"].model_copy(update={"legal_search_timeout_seconds": 0.05})
    response = api.post("/search/legal", json={"query": "receipt"})
    assert response.status_code == 503 and response.json()["status"] == "service_unavailable"


def test_health_and_probes(client, api):
    plain = client.get("/health").json()
    assert plain["status"] == "degraded" and plain["retrieval"] == {
        "enabled": False, "publication_loaded": False, "publication_id": None, "publication_name": None,
        "laws": None, "chunks": None, "models_ready": False, "answer_model_configured": False}
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200       # search disabled: nothing to load
    # With search enabled, readiness follows the in-memory publication.
    api.app.state.services = api.state["services"]
    ready = api.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["retrieval"]["laws"] == 5
    api.app.state.services = services(ScriptedModel(), snapshot=False)
    assert api.get("/health/ready").status_code == 503
    assert api.get("/health").json()["retrieval"]["publication_loaded"] is False


def test_oversized_bodies_are_refused_before_they_are_processed(api):
    huge = {"question": "ok?", "history": [{"role": "user", "content": "x" * 7000}] * 40}
    response = api.post("/ask", json={**huge, "law": "y" * 200}, headers={"X-Pad": "1"})
    assert response.status_code in {200, 422}                    # within the 1 MB JSON limit
    too_big = api.post("/ask", content=b'{"question": "' + b"x" * (1024 * 1024 + 10) + b'"}',
                       headers={"content-type": "application/json"})
    assert too_big.status_code == 413 and too_big.json()["detail"] == "Request body is too large."


def test_local_administration_is_rejected_before_any_upload_is_read(settings):
    app = create_app()
    remote = TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.9", 50000))
    with remote:
        for path in ("/local-admin/legal/uploads", "/contracts", "/local-admin/legal/publications"):
            response = remote.post(path, content=b"x" * 4096, headers={"content-type": "application/pdf"})
            assert response.status_code == 404, path
    spoofed = TestClient(app, base_url="http://attacker.example", client=("127.0.0.1", 50000))
    with spoofed:
        assert spoofed.post("/contracts", content=b"x").status_code == 404


def test_production_does_not_mount_admin_routes_or_docs(monkeypatch, settings):
    from app import main
    production = settings.model_copy(update={"environment": "production"})
    monkeypatch.setattr(main, "get_settings", lambda: production)
    with TestClient(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as hosted:
        assert hosted.get("/health/live").status_code == 200
        for path in ("/docs", "/redoc", "/openapi.json", "/contracts/abc",
                     "/local-admin/legal/uploads", "/local-admin/legal/publications"):
            assert hosted.get(path).status_code == 404, path
        assert hosted.post("/ask", json=QUESTION).status_code == 404       # search disabled in tests
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.create_app()) as local:
        assert local.get("/docs").status_code == 200 and local.get("/openapi.json").status_code == 200
    explicit = settings.model_copy(update={"environment": "production", "docs_enabled": True})
    monkeypatch.setattr(main, "get_settings", lambda: explicit)
    with TestClient(main.create_app()) as documented:
        assert documented.get("/openapi.json").status_code == 200


def test_unhandled_errors_do_not_leak_details(api):
    class Exploding:
        ask_slots = asyncio.Semaphore(1)

        @property
        def qa(self):
            raise RuntimeError("database password is hunter2")
    api.state["services"] = Exploding()
    with TestClient(api.app, raise_server_exceptions=False) as quiet:
        quiet.app.dependency_overrides = api.app.dependency_overrides
        response = quiet.post("/ask", json=QUESTION)
    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
