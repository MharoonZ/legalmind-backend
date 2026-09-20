from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["connected", "unavailable"]
    app: str
    version: str
    environment: str


class RetrievalHealthResponse(BaseModel):
    """Is the /ask pipeline actually ready to answer?

    `ready` is true only when there are passages to search, embeddings to
    search them with, and a configured language model.
    """

    ready: bool
    reasons: list[str] = Field(
        default_factory=list, description="Why the pipeline is not ready yet."
    )
    sources: int
    sections: int
    passages: int
    embedded_passages: int
    embedding_model: str
    embedding_model_loaded: bool
    embeddings_enabled: bool
    embedding_models_in_db: list[str]
    review_statuses_in_db: list[str]
    allowed_review_statuses: list[str]
    llm_provider: str
    llm_model: str
    llm_configured: bool
