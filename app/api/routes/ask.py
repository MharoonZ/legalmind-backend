from fastapi import APIRouter

from app.api.deps import AskServiceDep
from app.schemas.ask import AskRequest, AskResponse
from app.services.ask_service import load_fixtures

router = APIRouter(tags=["ask"])

_fixtures = load_fixtures()

_ASK_RESPONSE_EXAMPLES = {
    "supported": {
        "summary": "Question is supported by a stored passage",
        "value": _fixtures["supported"].model_dump(),
    },
    "unsupported": {
        "summary": "Question is not supported (refusal)",
        "value": _fixtures["unsupported"].model_dump(),
    },
}


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask a question about the Contract Act, 1872 (ss. 1-30)",
    responses={200: {"content": {"application/json": {"examples": _ASK_RESPONSE_EXAMPLES}}}},
)
async def ask(payload: AskRequest, service: AskServiceDep) -> AskResponse:
    """Send a question, get a structured JSON answer.

    - `supported: true`  -> answer with quote, section, page and evidence_id.
    - `supported: false` -> a clear refusal; evidence fields are null.
    """
    return await service.ask(payload.question)
