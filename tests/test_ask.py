"""End-to-end tests for POST /ask with fake retrieval and a fake LLM."""

import pytest
from pydantic import ValidationError

from app.core.errors import LLMError, RetrievalError
from app.schemas.ask import AskResponse, Citation
from app.services.llm.base import LLMAnswer
from tests.conftest import SECTION_10_ID, FakeLLM, FakeRepository, make_passage

EXPECTED_KEYS = {
    "supported",
    "answer",
    "quote",
    "section",
    "page",
    "evidence_id",
    "citations",
    "meta",
}


def test_supported_answer_has_the_sprint1_shape(api):
    res = api.post("/ask", json={"question": "What makes an agreement a contract?"})
    assert res.status_code == 200
    body = res.json()

    assert set(body) == EXPECTED_KEYS
    assert body["supported"] is True
    assert body["section"] == 10
    assert body["page"] == 14
    assert body["evidence_id"] == SECTION_10_ID
    # The flat fields must mirror the first citation.
    assert body["citations"][0]["evidence_id"] == body["evidence_id"]
    assert body["citations"][0]["quote"] == body["quote"]


def test_quote_comes_from_the_retrieved_passage(api):
    body = api.post("/ask", json={"question": "What is a contract?"}).json()
    assert body["quote"] in make_passage().text


def test_unsupported_when_nothing_is_retrieved(api):
    api.state["repository"] = FakeRepository(keyword=[], vector=[], sections=[])
    body = api.post("/ask", json={"question": "Can I patent a song melody?"}).json()

    assert body["supported"] is False
    assert body["citations"] == []
    assert body["quote"] is None and body["section"] is None
    assert "sections 1 to 30" in body["answer"]


def test_unsupported_when_evidence_is_too_weak(api):
    api.state["repository"] = FakeRepository(keyword=[make_passage(score=0.0001)])
    body = api.post("/ask", json={"question": "What is the weather today?"}).json()
    assert body["supported"] is False
    # The gate must run before the model, so the LLM is never called.
    assert api.state["llm"].prompts == []


def test_model_claiming_support_without_a_valid_citation_is_downgraded(api):
    api.state["llm"] = FakeLLM(
        LLMAnswer(
            supported=True,
            answer="Section 999 says so.",
            citations=[{"evidence_id": "ev:invented", "quote": "invented text"}],
        )
    )
    body = api.post("/ask", json={"question": "What makes a contract?"}).json()
    assert body["supported"] is False
    assert body["citations"] == []


def test_non_verbatim_quote_is_repaired_from_the_passage(api):
    api.state["llm"] = FakeLLM(
        LLMAnswer(
            supported=True,
            answer="Paraphrased.",
            citations=[{"evidence_id": SECTION_10_ID, "quote": "totally made up wording"}],
        )
    )
    body = api.post("/ask", json={"question": "What makes a contract?"}).json()
    assert body["supported"] is True
    assert body["quote"] in " ".join(make_passage().text.split())


def test_model_refusal_is_passed_through(api):
    api.state["llm"] = FakeLLM(
        LLMAnswer(supported=False, answer="The sections provided do not cover this.", citations=[])
    )
    body = api.post("/ask", json={"question": "Is arbitration binding?"}).json()
    assert body["supported"] is False
    assert body["answer"].startswith("The sections provided")


def test_question_naming_a_section_uses_direct_lookup(api):
    api.post("/ask", json={"question": "Explain section 10 please"})
    assert api.state["repository"].calls == ["sections"]


def test_question_naming_an_uncovered_section_is_refused_without_searching(api):
    body = api.post("/ask", json={"question": "What does section 124 say?"}).json()
    assert body["supported"] is False
    assert "124" in body["answer"]
    # Must not fall through to search: the word "section" matches unrelated
    # passages and would cite the wrong law back at the user.
    assert api.state["repository"].calls == []
    assert api.state["llm"].prompts == []


def test_retrieval_failure_returns_503(api):
    api.state["repository"] = FakeRepository(error=RetrievalError("db down"))
    res = api.post("/ask", json={"question": "What is consideration?"})
    assert res.status_code == 503


def test_llm_failure_returns_503(api):
    api.state["llm"] = FakeLLM(error=LLMError("gemini down"))
    res = api.post("/ask", json={"question": "What is consideration?"})
    assert res.status_code == 503


@pytest.mark.parametrize(
    "payload", [{}, {"question": ""}, {"question": "  "}, {"question": "ab"}, {"question": "???"}]
)
def test_bad_input_is_rejected(api, payload):
    assert api.post("/ask", json=payload).status_code == 422


def test_very_long_question_is_rejected(api):
    assert api.post("/ask", json={"question": "x" * 501}).status_code == 422


# ----------------------------------------------------------- schema contract
def _citation(**overrides) -> Citation:
    data = {
        "evidence_id": SECTION_10_ID,
        "section": 10,
        "section_title": "What agreements are contracts",
        "page": 14,
        "quote": "All agreements are contracts",
        "law_name": "Contract Act, 1872",
        "jurisdiction": "Pakistan",
        "source_url": "https://pakistancode.gov.pk/",
    }
    return Citation(**{**data, **overrides})


def test_supported_answer_needs_evidence():
    with pytest.raises(ValidationError):
        AskResponse(supported=True, answer="text")


def test_supported_answer_needs_a_citation():
    with pytest.raises(ValidationError):
        AskResponse(
            supported=True,
            answer="text",
            quote="q",
            section=1,
            page=1,
            evidence_id="x",
            citations=[],
        )


def test_flat_fields_must_mirror_the_first_citation():
    with pytest.raises(ValidationError):
        AskResponse(
            supported=True,
            answer="text",
            quote="different quote",
            section=10,
            page=14,
            evidence_id=SECTION_10_ID,
            citations=[_citation()],
        )


def test_unsupported_answer_must_not_have_evidence():
    with pytest.raises(ValidationError):
        AskResponse(supported=False, answer="no", quote="q", section=1, page=1, evidence_id="x")


def test_openapi_lists_routes(api):
    paths = api.get("/openapi.json").json()["paths"]
    assert "/ask" in paths and "/health" in paths and "/health/retrieval" in paths
