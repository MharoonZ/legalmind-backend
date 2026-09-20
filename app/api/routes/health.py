from fastapi import APIRouter, HTTPException, status

from app.api.deps import DatabaseDep, EmbeddingServiceDep, LawRepositoryDep, SettingsDep
from app.core.errors import RetrievalError
from app.schemas.health import HealthResponse, RetrievalHealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(db: DatabaseDep, settings: SettingsDep) -> HealthResponse:
    """Always answers 200 while the app is running.

    `status` is "degraded" when the database cannot be reached; /ask then
    returns 503, because there is no corpus to ground an answer in.
    """
    db_ok = await db.ping()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "unavailable",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get(
    "/health/retrieval",
    response_model=RetrievalHealthResponse,
    summary="Is the /ask pipeline ready (corpus, embeddings, LLM)?",
)
async def retrieval_health(
    repository: LawRepositoryDep,
    embeddings: EmbeddingServiceDep,
    settings: SettingsDep,
) -> RetrievalHealthResponse:
    """One call that tells you which setup step is still missing."""
    try:
        stats = await repository.stats()
    except RetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Legal corpus is temporarily unavailable.",
        ) from exc

    reasons: list[str] = []
    if stats.passages == 0:
        reasons.append("No passages imported. Run scripts/ingest_contract_act_corpus.py")
    if not set(settings.review_statuses_list) & set(stats.review_statuses):
        reasons.append(
            "No source is in an allowed review status "
            f"({settings.retrieval_review_statuses})"
        )
    if settings.embeddings_enabled and stats.embedded_passages == 0:
        reasons.append("No embeddings stored. Run scripts/build_embeddings.py")
    if (
        settings.embeddings_enabled
        and stats.embedding_models
        and settings.embedding_model not in stats.embedding_models
    ):
        reasons.append(
            f"Stored embeddings use {', '.join(stats.embedding_models)}, "
            f"but EMBEDDING_MODEL is {settings.embedding_model}"
        )
    if not settings.llm_configured:
        reasons.append(f"{settings.llm_api_key_name} is not set")

    return RetrievalHealthResponse(
        ready=not reasons,
        reasons=reasons,
        sources=stats.sources,
        sections=stats.sections,
        passages=stats.passages,
        embedded_passages=stats.embedded_passages,
        embedding_model=settings.embedding_model,
        embedding_model_loaded=embeddings.is_loaded,
        embeddings_enabled=settings.embeddings_enabled,
        embedding_models_in_db=list(stats.embedding_models),
        review_statuses_in_db=list(stats.review_statuses),
        allowed_review_statuses=settings.review_statuses_list,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_configured=settings.llm_configured,
    )
