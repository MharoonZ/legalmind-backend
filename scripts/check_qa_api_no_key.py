"""Exercise the real /ask route without an AI request or database write.

Run from the backend root with its virtual environment. The empty key must be
set inside Python: PowerShell removes an environment variable assigned "",
which would let pydantic-settings fall back to the key in .env.
"""
import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["QA_RECORD_RUNS"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.qa_generation_config import get_answer_settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main():
    settings = get_answer_settings()
    if settings.openai_api_key and settings.openai_api_key.get_secret_value():
        raise RuntimeError("The AI key was not disabled; refusing to run.")
    if settings.qa_record_runs:
        raise RuntimeError("Audit writes were not disabled; refusing to run.")
    with TestClient(create_app()) as client:
        response = client.post("/ask", json={
            "question": "Section 19 of the Consumer Protection Act",
        })
    body = response.json()
    print("HTTP:", response.status_code)
    print("Status:", body.get("status"))
    print("Citations:", len(body.get("citations", [])))
    if response.status_code != 503 or body.get("status") != "service_unavailable":
        raise SystemExit("Expected a provider-unavailable response with no AI key.")


if __name__ == "__main__":
    main()
