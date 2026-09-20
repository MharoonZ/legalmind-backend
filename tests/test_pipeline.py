"""Unit tests for the retrieval, fusion, citation and LLM building blocks."""

from dataclasses import replace

import httpx
import pytest

from app.core.errors import LLMError
from app.repositories.law_repository import keyword_terms
from app.services.citations import repair_quote, validate_citations
from app.services.fusion import reciprocal_rank_fusion
from app.services.llm.base import parse_llm_json
from app.services.prompt import SYSTEM_PROMPT, build_user_prompt
from app.services.question_parser import parse_question
from tests.conftest import SECTION_10_ID, SECTION_10_TEXT, make_passage


# ----------------------------------------------------------- question parser
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What does section 10 say?", (10,)),
        ("explain sec. 25", (25,)),
        ("s. 2 definitions", (2,)),
        ("compare sections 10 and 11", (10, 11)),
        ("sections 5 to 7", (5, 6, 7)),
        ("u/s 23 what is unlawful", (23,)),
        ("§14 free consent", (14,)),
        ("what is a contract", ()),
        ("I paid 500 rupees for 10 items", ()),
    ],
)
def test_section_detection(question, expected):
    assert parse_question(question).section_numbers == expected


def test_sections_outside_the_corpus_are_reported_separately():
    parsed = parse_question("what about section 124?")
    assert parsed.section_numbers == ()
    assert parsed.out_of_range_sections == (124,)


def test_keyword_terms_drop_stopwords():
    assert keyword_terms("What is the meaning of consideration?") == [
        "meaning",
        "consideration",
    ]


# -------------------------------------------------------------------- fusion
def test_rrf_prefers_a_passage_found_by_both_retrievers():
    both = make_passage("ev:both", section_number=10)
    keyword_only = make_passage("ev:kw", section_number=11)
    vector_only = make_passage("ev:vec", section_number=12, retriever="vector")

    fused = reciprocal_rank_fusion(
        [
            [keyword_only, both],
            [vector_only, replace(both, retriever="vector")],
        ],
        k=60,
        top_k=3,
    )
    assert set(fused[0].retrievers) == {"keyword", "vector"}
    assert fused[0].evidence_id == "ev:both"
    assert fused[0].rrf_score > fused[1].rrf_score


def test_rrf_is_deterministic_for_equal_scores():
    first = make_passage("ev:a", section_number=3)
    second = make_passage("ev:b", section_number=1)
    fused = reciprocal_rank_fusion([[first], [second]], top_k=2)
    # Same RRF score -> ordered by section, then page.
    assert [hit.passage.section_number for hit in fused] == [1, 3]


def test_rrf_respects_top_k():
    hits = [make_passage(f"ev:{i}", section_number=i) for i in range(1, 11)]
    assert len(reciprocal_rank_fusion([hits], top_k=5)) == 5


# ----------------------------------------------------------------- citations
def test_verbatim_quote_is_kept():
    quote, verbatim = repair_quote("All agreements are contracts", SECTION_10_TEXT)
    assert verbatim is True
    assert quote == "All agreements are contracts"


def test_quote_with_different_whitespace_still_counts_as_verbatim():
    quote, verbatim = repair_quote("All  agreements\nare contracts", SECTION_10_TEXT)
    assert verbatim is True
    assert quote == "All agreements are contracts"


def test_partly_invented_quote_falls_back_to_real_text():
    quote, verbatim = repair_quote(
        "All agreements are contracts if they are made by aliens from Mars", SECTION_10_TEXT
    )
    assert verbatim is False
    assert quote in " ".join(SECTION_10_TEXT.split())


def test_fully_invented_quote_falls_back_to_real_text():
    quote, verbatim = repair_quote("nothing like the source at all", SECTION_10_TEXT)
    assert verbatim is False
    assert quote in " ".join(SECTION_10_TEXT.split())


def test_fallback_quote_is_not_just_the_section_number():
    # Statute text opens with "29. Agreements void for uncertainty." - stopping
    # at the first full stop would quote "29." and prove nothing.
    passage = "29. Agreements void for uncertainty. Agreements, the meaning of which is not certain, are void."
    quote, _ = repair_quote("completely unrelated wording here", passage)
    assert len(quote) > 40
    assert quote != "29."


def test_unknown_evidence_ids_are_dropped():
    retrieved = {SECTION_10_ID: make_passage()}
    validated = validate_citations(
        [
            {"evidence_id": "ev:hallucinated", "quote": "anything"},
            {"evidence_id": SECTION_10_ID, "quote": "All agreements are contracts"},
        ],
        retrieved,
    )
    assert [c.passage.evidence_id for c in validated] == [SECTION_10_ID]


def test_duplicate_and_malformed_citations_are_ignored():
    retrieved = {SECTION_10_ID: make_passage()}
    validated = validate_citations(
        [
            {"evidence_id": SECTION_10_ID, "quote": "All agreements are contracts"},
            {"evidence_id": SECTION_10_ID, "quote": "All agreements are contracts"},
            "not a dict",
            {},
        ],
        retrieved,
    )
    assert len(validated) == 1


def test_citations_are_capped():
    retrieved = {f"ev:{i}": make_passage(f"ev:{i}", section_number=i) for i in range(1, 6)}
    validated = validate_citations(
        [{"evidence_id": f"ev:{i}", "quote": "All agreements are contracts"} for i in range(1, 6)],
        retrieved,
        max_citations=3,
    )
    assert len(validated) == 3


# -------------------------------------------------------------------- prompt
def test_prompt_contains_evidence_ids_and_the_question():
    from app.services.fusion import FusedHit

    hits = [FusedHit(passage=make_passage())]
    prompt = build_user_prompt("What is a contract?", hits)
    assert SECTION_10_ID in prompt
    assert "What is a contract?" in prompt
    assert "section: 10 - What agreements are contracts" in prompt
    assert "pdf_page: 14" in prompt


def test_system_prompt_forbids_outside_knowledge():
    assert "ONLY from the EVIDENCE" in SYSTEM_PROMPT


# ---------------------------------------------------------------- LLM client
def test_parse_llm_json_handles_markdown_fences():
    answer = parse_llm_json('```json\n{"supported": true, "answer": "hi", "citations": []}\n```')
    assert answer.supported is True and answer.answer == "hi"


def test_parse_llm_json_rejects_non_json():
    with pytest.raises(LLMError):
        parse_llm_json("I think the answer is yes.")


def test_parse_llm_json_rejects_empty_answer():
    with pytest.raises(LLMError):
        parse_llm_json('{"supported": true, "answer": "", "citations": []}')


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ------------------------------------------------------------- OpenAI client
def _openai_with(handler, **kwargs):
    from app.services.llm.openai import OpenAIClient

    return OpenAIClient(
        "key",
        model=kwargs.pop("model", "gpt-4o-mini"),
        base_url="https://example.invalid/v1",
        max_retries=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


@pytest.mark.anyio
async def test_openai_parses_a_successful_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        content = '{"supported": true, "answer": "ok", "citations": []}'
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        )

    client = _openai_with(handler)
    answer = await client.generate("sys", "user")
    assert answer.answer == "ok"
    assert seen["auth"] == "Bearer key"
    assert seen["body"]["response_format"]["json_schema"]["strict"] is True
    assert seen["body"]["temperature"] == 0.1
    await client.aclose()


@pytest.mark.anyio
async def test_openai_omits_temperature_for_reasoning_models():
    import json

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        content = '{"supported": false, "answer": "no", "citations": []}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = _openai_with(handler, model="gpt-5-mini")
    await client.generate("sys", "user")
    assert "temperature" not in seen["body"]
    await client.aclose()


@pytest.mark.anyio
async def test_openai_out_of_credit_is_not_retried_and_explains_itself():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text='{"error": {"code": "insufficient_quota"}}')

    client = _openai_with(handler)
    with pytest.raises(LLMError, match="no credit"):
        await client.generate("sys", "user")
    assert calls["n"] == 1
    await client.aclose()


@pytest.mark.anyio
async def test_openai_bad_key_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="invalid api key")

    client = _openai_with(handler)
    with pytest.raises(LLMError, match="401"):
        await client.generate("sys", "user")
    assert calls["n"] == 1
    await client.aclose()


@pytest.mark.anyio
async def test_openai_retries_rate_limits_then_fails():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down")

    client = _openai_with(handler)
    with pytest.raises(LLMError):
        await client.generate("sys", "user")
    assert calls["n"] == 2
    await client.aclose()


def test_openai_settings_accept_both_env_names():
    from app.core.config import Settings

    base = {"database_url": "postgresql://a:b@localhost/x"}
    assert Settings(**base, openai_api_key="k").llm_configured is True
    assert Settings(**base, openai_api_key="").llm_configured is False
    assert Settings(**base, openai_api_key="k").llm_model == "gpt-4o-mini"


def test_llm_env_aliases_still_work(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "old-name-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-x")
    settings = Settings(database_url="postgresql://a:b@localhost/x")
    assert settings.openai_api_key == "old-name-key"
    assert settings.openai_model == "gpt-x"


def test_base_url_trailing_slash_is_removed():
    from app.core.config import Settings

    settings = Settings(
        database_url="postgresql://a:b@localhost/x", openai_base_url="https://api.openai.com/v1/"
    )
    assert settings.llm_base_url == "https://api.openai.com/v1"


def test_factory_builds_an_openai_client():
    from app.core.config import Settings
    from app.services.llm.factory import build_llm_client
    from app.services.llm.openai import OpenAIClient

    client = build_llm_client(
        Settings(database_url="postgresql://a:b@localhost/x", openai_api_key="k")
    )
    assert isinstance(client, OpenAIClient) and client.configured


# ----------------------------------------------------- OpenAI auto-adaptation
_OK = {"choices": [{"message": {"content": '{"supported": true, "answer": "ok", "citations": []}'}}]}


@pytest.mark.anyio
async def test_falls_back_from_json_schema_to_json_object_then_none():
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        fmt = body.get("response_format", {}).get("type")
        if fmt in ("json_schema", "json_object"):
            return httpx.Response(400, text=f"response_format {fmt} is not supported")
        return httpx.Response(200, json=_OK)

    client = _openai_with(handler)
    answer = await client.generate("sys", "user")
    assert answer.answer == "ok"
    assert [b.get("response_format", {}).get("type") for b in seen] == [
        "json_schema",
        "json_object",
        None,
    ]
    # The lesson is remembered: the next call goes straight to plain mode.
    await client.generate("sys", "user")
    assert "response_format" not in seen[-1]
    await client.aclose()


@pytest.mark.anyio
async def test_switches_token_parameter_when_the_provider_wants_max_tokens():
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "max_completion_tokens" in body:
            return httpx.Response(400, text="unknown parameter: max_completion_tokens")
        return httpx.Response(200, json=_OK)

    client = _openai_with(handler)
    await client.generate("sys", "user")
    assert "max_tokens" in seen[-1] and "max_completion_tokens" not in seen[-1]
    await client.aclose()


@pytest.mark.anyio
async def test_drops_temperature_when_the_model_refuses_it():
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "temperature" in body:
            return httpx.Response(400, text="temperature is not supported for this model")
        return httpx.Response(200, json=_OK)

    client = _openai_with(handler)
    await client.generate("sys", "user")
    assert "temperature" not in seen[-1]
    await client.aclose()


def test_reasoning_model_think_blocks_are_stripped():
    raw = '<think>let me reason about this...</think>\n{"supported": true, "answer": "hi", "citations": []}'
    assert parse_llm_json(raw).answer == "hi"
