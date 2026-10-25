"""Question answering from the active, approved legal publication."""
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.deps import DatabaseDep, SettingsDep
from app.core.qa_generation_config import get_answer_settings
from app.repositories.legal_search_repository import LegalSearchRepository
from app.repositories.qa_context_repository import QaContextRepository
from app.schemas.qa import AskIn, AskOut
from app.services.legal_retrieval import LegalRetrievalService
from app.services.qa_answer import QuestionAnswerService
from app.services.qa_citations import CitationVerifier
from app.services.qa_context import ContextExpander
from app.services.qa_evidence_gate import EvidenceGate
from app.services.qa_generation import AnswerGenerator
from app.services.qa_preparation import QuestionPreparationService
from app.services.qa_reranker import CandidateReranker
from app.services.qa_source_verification import QaSourceVerifier


router = APIRouter(tags=["legal answers"])


@lru_cache(maxsize=1)
def _generator():
    return AnswerGenerator(get_answer_settings())


def get_fact_requirement_provider(request: Request):
    # Server configuration only; /ask cannot submit its own rule requirements.
    return getattr(request.app.state, "qa_fact_requirement_provider", None)


def get_question_answer_service(db: DatabaseDep, settings: SettingsDep,
                               fact_provider=Depends(get_fact_requirement_provider)):
    if not settings.legal_search_enabled:
        raise HTTPException(404, detail="Not found")
    answer_settings = get_answer_settings()
    retrieval = LegalRetrievalService(LegalSearchRepository(db), settings)
    preparation = QuestionPreparationService(
        retrieval, CandidateReranker(), ContextExpander(QaContextRepository(db)), EvidenceGate(),
        fact_provider=fact_provider)
    recorder = None
    if answer_settings.qa_record_runs:
        from app.repositories.qa_record_repository import QaRecordRepository
        recorder = QaRecordRepository(db, answer_settings.openai_model)
    return QuestionAnswerService(
        preparation, _generator(), CitationVerifier(QaSourceVerifier(db)),
        repair_attempts=answer_settings.qa_repair_attempts, recorder=recorder)


@router.post("/ask", response_model=AskOut)
async def ask(payload: AskIn, service=Depends(get_question_answer_service)):
    result = await service.ask(payload)
    if result.status == "service_unavailable":
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result
