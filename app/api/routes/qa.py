"""POST /ask: a cited answer from the active, approved legal publication."""
import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep, enforce_rate_limit, get_services, require_api_key
from app.qa.service import QuestionAnswerService
from app.schemas.qa import AskIn, AskOut

router = APIRouter(tags=["legal answers"], dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
logger = logging.getLogger(__name__)


def _timing_header(timings: dict[str, float]) -> str:
    return ", ".join(f"{name};dur={value:.0f}" for name, value in timings.items())


@router.post("/ask", response_model=AskOut, responses={503: {"model": AskOut}},
             summary="Answer a legal question from the published collection")
async def ask(payload: AskIn, settings: SettingsDep, services=Depends(get_services)):
    """Returns a grounded answer whose ``[n]`` markers point at ``citations[n-1]``.

    A provider or database failure is a 503 with the same response shape, so a
    client never mistakes an outage for "the law does not say".
    """
    timings: dict[str, float] = {}
    try:
        async with asyncio.timeout(settings.qa_queue_timeout_seconds or 0.001):
            await services.ask_slots.acquire()
    except TimeoutError:
        result = QuestionAnswerService.unavailable(
            payload.question, reason="The answer service is busy. Please retry shortly.")
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"),
                            headers={"Retry-After": "5"})
    try:
        async with asyncio.timeout(settings.qa_request_timeout_seconds):
            result, timings = await services.qa.ask(payload, record=False)
    except TimeoutError:
        logger.warning("qa_request_timeout seconds=%s", settings.qa_request_timeout_seconds)
        result = QuestionAnswerService.unavailable(payload.question, reason="The answer request timed out.")
    finally:
        services.ask_slots.release()
    headers = {"Server-Timing": _timing_header(timings)} if timings else {}
    status_code = 503 if result.status == "service_unavailable" else 200
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"), headers=headers)
