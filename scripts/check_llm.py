"""Check that your OpenAI key and model work, with the real error if not.

    python scripts/check_llm.py

Uses OPENAI_API_KEY and OPENAI_MODEL from .env and sends one tiny request
through the same client /ask uses. If this prints a reply, /ask can reach the
model. If it fails, the message says why (bad key, wrong model, no credit).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import LLMError  # noqa: E402
from app.services.llm.factory import build_llm_client  # noqa: E402


async def main() -> int:
    settings = get_settings()
    print(f"OpenAI model: {settings.llm_model}")
    if not settings.llm_configured:
        print(f"{settings.llm_api_key_name} is empty. Put it in .env first.")
        return 1

    client = build_llm_client(settings)
    try:
        answer = await client.generate(
            "Reply with JSON only.",
            'Return {"supported": true, "answer": "pong", "citations": []}',
        )
        print(f"OK - model replied: {answer.answer!r}")
        return 0
    except LLMError as exc:
        print(f"FAIL - {exc}")
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
