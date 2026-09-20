"""POST /ask - grounded question answering over the Contract Act, 1872."""

import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AskServiceDep
from app.core.errors import LLMError, RetrievalError
from app.schemas.ask import AskRequest, AskResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])

_ASK_RESPONSE_EXAMPLES = {
    "supported": {
        "summary": "Answer backed by a stored passage",
        "value": {
            "supported": True,
            "answer": (
                "An agreement becomes a contract when the parties consent freely, "
                "are competent to contract, and the agreement has lawful "
                "consideration and a lawful object, and is not expressly declared void."
            ),
            "quote": (
                "All agreements are contracts if they are made by the free consent of "
                "parties competent to contract, for a lawful consideration and with a "
                "lawful object, and are not hereby expressly declared to be void."
            ),
            "section": 10,
            "page": 14,
            "evidence_id": "ev:23d0191582f0ea2eaaa28406a652082b22a8f191e051ffc670a46fb901e01ae2",
            "citations": [
                {
                    "evidence_id": "ev:23d0191582f0ea2eaaa28406a652082b22a8f191e051ffc670a46fb901e01ae2",
                    "section": 10,
                    "section_title": "What agreements are contracts",
                    "page": 14,
                    "quote": "All agreements are contracts if they are made by the free consent of parties competent to contract, for a lawful consideration and with a lawful object, and are not hereby expressly declared to be void.",
                    "law_name": "Contract Act, 1872",
                    "jurisdiction": "Pakistan",
                    "source_url": "https://pakistancode.gov.pk/",
                }
            ],
            "meta": None,
        },
    },
    "unsupported": {
        "summary": "No supporting passage (clear refusal)",
        "value": {
            "supported": False,
            "answer": (
                "I can only answer questions that are supported by sections 1 to 30 of "
                "the Contract Act, 1872. I could not find support for this question in "
                "those sections."
            ),
            "quote": None,
            "section": None,
            "page": None,
            "evidence_id": None,
            "citations": [],
            "meta": None,
        },
    },
}


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask a question about the Contract Act, 1872 (ss. 1-30)",
    responses={
        200: {"content": {"application/json": {"examples": _ASK_RESPONSE_EXAMPLES}}},
        422: {"description": "Question missing, too short or too long"},
        503: {"description": "Corpus search or the language model is unavailable"},
    },
)
async def ask(payload: AskRequest, service: AskServiceDep) -> AskResponse:
    """Send a question, get a structured JSON answer.

    - `supported: true`  -> answer plus verified citations.
    - `supported: false` -> a clear refusal; evidence fields are null.

    A 503 means *our* pipeline is down, which is deliberately different from a
    refusal: the frontend should offer a retry, not tell the user the law is
    silent on the matter.
    """
    try:
        return await service.ask(payload.question)
    except RetrievalError as exc:
        logger.error("Retrieval unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Legal corpus is temporarily unavailable. Please try again.",
        ) from exc
    except LLMError as exc:
        logger.error("LLM unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The answering model is temporarily unavailable. Please try again.",
        ) from exc
