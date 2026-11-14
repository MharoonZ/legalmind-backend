"""Exercise the real API and DB citation path with an offline Gemini response."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx


async def verify(output: Path):
    os.environ.update(LEGAL_SEARCH_ENABLED="true", LEGAL_INGEST_ENABLED="false",
                      QA_RECORD_RUNS="false", QA_RERANK_WARM_ON_STARTUP="false",
                      LLM_PROVIDER="gemini", GEMINI_API_KEY="", OPENAI_API_KEY="")
    from app.api.routes import qa
    from app.core.qa_generation_config import AnswerSettings
    from app.main import create_app
    from app.services.qa.generation import AnswerGenerator

    calls = []

    def provider(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        body = json.loads(request.content)
        assert body["systemInstruction"]["parts"]
        data = json.loads(body["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
        unit = next(unit for unit in data["evidence"] if unit["relation"] == "retrieved")
        # Use only the source-mapped body, excluding the display heading.
        quote = "Every manufacturer or trader who sells any goods shall issue to the purchaser a receipt showing –"
        assert quote in " ".join(unit["text"].split())
        partial = bool(data["unavailable_requested_references"])
        draft = {
            "outcome": "partial" if partial else "answered", "answer": "Discard this unverified prose.",
            "claims": [{"evidence_id": unit["evidence_id"], "quote": quote, "claim_text": quote}],
            "missing_support": "Missing requested source." if partial else "", "clarification_question": None,
        }
        calls.append(data["question"])
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps(draft)}]}}]})

    adapter = AnswerGenerator(AnswerSettings(_env_file=None, llm_provider="gemini",
        gemini_api_key="offline-test-credential"), gemini_transport=httpx.MockTransport(provider))
    qa._generator = lambda: adapter
    app = create_app()
    results = []
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        for question, expected in [
            ("What is Section 19 of the Consumer Protection Act?", "answered"),
            ("What do sections 19 and 999 of the Consumer Protection Act say?", "partial"),
            ("What is Section 999 of the Consumer Protection Act?", "insufficient_evidence"),
            ("What is Section 19?", "clarification_needed"),
        ]:
            response = await client.post("/ask", json={"question": question})
            body = response.json()
            assert response.status_code == 200 and body["status"] == expected, body["status"]
            assert "unverified prose" not in body["answer"]
            if expected in {"answered", "partial"}:
                assert body["claims"] and body["citations"]
                assert all(c["filename"] == "477.pdf" and c["page_index"] == 7 for c in body["citations"])
            results.append({"question": question, "http_status": response.status_code, "response": body})
        invalid = await client.post("/ask", json={"question": "!!!"})
        assert invalid.status_code == 422
    assert len(calls) == 2
    output.write_text(json.dumps({"provider": "offline HTTP fixture", "database": "configured real DB",
                                 "cases": results, "invalid_http": 422}, indent=2), encoding="utf-8")
    print("Verified 4 API outcomes, exact PDF citations and input validation.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    try:
        asyncio.run(verify(parser.parse_args().output))
    except Exception as exc:
        raise SystemExit(f"Read-only pipeline verification failed ({type(exc).__name__}).") from None
