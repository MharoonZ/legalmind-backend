from types import SimpleNamespace
import asyncio

import pytest

from app.api.routes.legal_search import get_legal_retrieval_service
from app.core.config import get_settings
from app.schemas.legal_search import LegalSearchOut


def test_search_is_disabled_by_default(client):
    response = client.post("/search/legal", json={"query": "receipt"})
    assert response.status_code == 404


def test_search_endpoint_returns_candidates_alongside_phase_c_ask(client):
    async def search(payload):
        return LegalSearchOut(status="no_results", query=payload.query, retrieval_query=payload.query)
    client.app.dependency_overrides[get_legal_retrieval_service] = lambda: SimpleNamespace(search=search)
    response = client.post("/search/legal", json={"query": "receipt"})
    assert response.status_code == 200
    assert response.json()["status"] == "no_results" and response.json()["results"] == []
    assert "/ask" in client.app.openapi()["paths"]


def test_search_timeout_returns_structured_503(client):
    async def search(payload):
        await asyncio.sleep(1)
        pytest.fail("The request should have timed out.")

    settings = get_settings().model_copy(update={"legal_search_timeout_seconds": .01})
    client.app.dependency_overrides[get_settings] = lambda: settings
    client.app.dependency_overrides[get_legal_retrieval_service] = lambda: SimpleNamespace(search=search)
    result = client.post("/search/legal", json={"query": "receipt"})
    assert result.status_code == 503 and result.json()["status"] == "service_unavailable"


@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "receipt", "limit": 999},
    {"query": "receipt", "revision_ids": ["unpublished"]},
    {"query": "receipt", "context": {"previous_query": "x" * 4001}}])
def test_search_http_contract_rejects_invalid_or_unscoped_payload(client, payload):
    client.app.dependency_overrides[get_legal_retrieval_service] = lambda: None
    assert client.post("/search/legal", json=payload).status_code == 422


@pytest.mark.parametrize("path,payload", [
    ("/local-admin/legal/publications", {"name": "test", "chunk_set_ids": []}),
    ("/local-admin/legal/publications/validate", {"name": "test", "chunk_set_ids": []}),
    ("/local-admin/legal/publications/00000000-0000-0000-0000-000000000000/activate",
     {"expected_manifest_sha256": "a"*64})])
def test_publication_mutations_keep_existing_local_admin_gate(client, path, payload):
    assert client.post(path, json=payload).status_code == 404
