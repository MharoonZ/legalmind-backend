"""One live, read-only /ask request against the configured publication and LLM.

Run only after replacing any key exposed outside the backend .env. This script
does not print the key, question text, generated answer, or source quotations.
"""
import os

os.environ["QA_RECORD_RUNS"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.qa_generation_config import get_answer_settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main():
    settings = get_answer_settings()
    if settings.qa_record_runs:
        raise RuntimeError("Audit writes were not disabled; refusing to run.")
    if not settings.openai_api_key or not settings.openai_api_key.get_secret_value():
        raise RuntimeError("A server-side API key is required for this live check.")
    with TestClient(create_app()) as client:
        response = client.post("/ask", json={
            "question": "What does Section 19 of the Consumer Protection Act say?",
        })
    body = response.json()
    print("HTTP:", response.status_code)
    print("Status:", body.get("status"))
    print("Claims:", len(body.get("claims", [])))
    print("Citations:", len(body.get("citations", [])))
    print("Source pages:", [row.get("page_index") for row in body.get("citations", [])])
    if response.status_code != 200 or body.get("status") not in {"answered", "partial"}:
        raise SystemExit("Live answer did not return a verified, cited response.")
    if not body.get("claims") or not body.get("citations"):
        raise SystemExit("Live answer lacks a claim or citation.")


if __name__ == "__main__":
    main()
