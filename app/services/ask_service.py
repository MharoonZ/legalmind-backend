"""Answer service for POST /ask.

Sprint 1, week 1: this returns fixture JSON only. There is no retrieval and no
answer generation yet.

Later (week 2) only this file changes: it will call Haroon's retrieval, build
the answer from the passages, and return the same `AskResponse` shape.
The route and the schema stay the same, so the frontend is not affected.
"""

import json
from functools import lru_cache
from pathlib import Path

from app.schemas.ask import AskResponse

FIXTURES_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "ask_fixtures.json"


@lru_cache
def load_fixtures() -> dict[str, AskResponse]:
    """Read and validate the fixture file one time."""
    raw = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return {
        "supported": AskResponse.model_validate(raw["supported"]),
        "unsupported": AskResponse.model_validate(raw["unsupported"]),
    }


class AskService:
    async def ask(self, question: str) -> AskResponse:
        # `question` is not used yet; it will feed retrieval in week 2.
        return load_fixtures()["supported"]
