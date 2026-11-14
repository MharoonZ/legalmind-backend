"""Question answering from the active, approved legal publication."""
import asyncio
import logging
from functools import lru_cache
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.api.deps import DatabaseDep, SettingsDep
from app.core.qa_generation_config import get_answer_settings
from app.repositories.legal_search_repository import LegalSearchRepository
from app.repositories.qa_context_repository import QaContextRepository
from app.schemas.qa import AskIn, AskOut
from app.services.legal_retrieval import LegalRetrievalService
from app.services.qa.answer import QuestionAnswerService
from app.services.qa.citations import CitationVerifier
from app.services.qa.context import ContextExpander
from app.services.qa.generation import AnswerGenerator
from app.services.qa.preparation import QuestionPreparationService
from app.services.qa.reranker import CandidateReranker
from app.services.qa.source_verification import QaSourceVerifier


router = APIRouter(tags=["legal answers"])
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _generator():
    return AnswerGenerator(get_answer_settings())


def get_question_answer_service(db: DatabaseDep, settings: SettingsDep):
    if not settings.legal_search_enabled:
        raise HTTPException(404, detail="Not found")
    answer_settings = get_answer_settings()
    retrieval = LegalRetrievalService(LegalSearchRepository(db), settings)
    preparation = QuestionPreparationService(
        retrieval, CandidateReranker(), ContextExpander(QaContextRepository(db)))
    recorder = None
    if answer_settings.qa_record_runs:
        from app.repositories.qa_record_repository import QaRecordRepository
        recorder = QaRecordRepository(db, answer_settings.active_model)
    return QuestionAnswerService(
        preparation, _generator(), CitationVerifier(QaSourceVerifier(db)),
        repair_attempts=answer_settings.qa_repair_attempts, recorder=recorder)


@router.post("/ask", response_model=AskOut, responses={503: {"model": AskOut}})
async def ask(payload: AskIn, settings: SettingsDep, service=Depends(get_question_answer_service)):
    started = perf_counter()
    try:
        async with asyncio.timeout(settings.qa_request_timeout_seconds):
            result = await service.ask(payload)
    except TimeoutError:
        result = QuestionAnswerService.unavailable(payload.question, reason="The answer request timed out.")
    logger.info("qa_request outcome=%s elapsed_ms=%.0f", result.status, (perf_counter() - started) * 1000)
    if result.status == "service_unavailable":
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result
