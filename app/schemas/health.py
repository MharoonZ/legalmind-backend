from typing import Literal

from pydantic import BaseModel


class RetrievalHealth(BaseModel):
    enabled: bool
    publication_loaded: bool = False
    publication_id: str | None = None
    publication_name: str | None = None
    laws: int | None = None
    chunks: int | None = None
    models_ready: bool = False
    answer_model_configured: bool = False


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["connected", "unavailable"]
    app: str
    version: str
    environment: str
    retrieval: RetrievalHealth | None = None
