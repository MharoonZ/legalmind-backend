import pytest
from pydantic import ValidationError

from app.schemas.ask import AskResponse

EXPECTED_KEYS = {"supported", "answer", "quote", "section", "page", "evidence_id"}


def test_ask_returns_contract_shape(client):
    res = client.post("/ask", json={"question": "What makes an agreement a contract?"})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == EXPECTED_KEYS
    assert body["supported"] is True
    assert body["section"] == 10
    assert body["evidence_id"]


def test_ask_works_even_when_db_is_down(client):
    # Fixtures only for now, so /ask must not depend on the database.
    assert client.post("/ask", json={"question": "What is consideration?"}).status_code == 200


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "  "}, {"question": "ab"}])
def test_ask_rejects_bad_input(client, payload):
    assert client.post("/ask", json=payload).status_code == 422


def test_ask_rejects_very_long_question(client):
    assert client.post("/ask", json={"question": "x" * 501}).status_code == 422


def test_supported_answer_needs_evidence():
    with pytest.raises(ValidationError):
        AskResponse(supported=True, answer="text")


def test_unsupported_answer_must_not_have_evidence():
    with pytest.raises(ValidationError):
        AskResponse(supported=False, answer="no", quote="q", section=1, page=1, evidence_id="x")


def test_openapi_lists_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/ask" in paths and "/health" in paths
