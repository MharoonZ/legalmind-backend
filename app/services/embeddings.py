"""Sentence-transformers embeddings.

The same model must produce the passage vectors (scripts/build_embeddings.py)
and the question vector (/ask); the model name is stored next to every vector
so the two can never drift apart.

The model is loaded lazily and encoded in a worker thread, because
sentence-transformers is synchronous and would otherwise block the event loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import anyio.to_thread

from app.core.errors import EmbeddingError

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Thin, thread-safe wrapper around one SentenceTransformer model."""

    def __init__(
        self,
        model_name: str,
        *,
        dimension: int,
        device: str = "cpu",
        batch_size: int = 32,
        enabled: bool = True,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self.device = device
        self.batch_size = batch_size
        self.enabled = enabled
        self._model: Any | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> Any:
        """Load the model once. Safe to call from several threads."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on install
                raise EmbeddingError(
                    "sentence-transformers is not installed; "
                    "run `pip install -r requirements.txt` or set EMBEDDINGS_ENABLED=false"
                ) from exc
            try:
                model = SentenceTransformer(self.model_name, device=self.device)
            except Exception as exc:  # noqa: BLE001 - network/disk/model errors
                raise EmbeddingError(
                    f"Could not load embedding model '{self.model_name}': {exc}"
                ) from exc

            actual = int(model.get_sentence_embedding_dimension())
            if actual != self.dimension:
                raise EmbeddingError(
                    f"Model '{self.model_name}' produces {actual} dimensions but the "
                    f"database column is {self.dimension}. Set EMBEDDING_DIM={actual} "
                    "and run the Alembic migration again."
                )
            self._model = model
            logger.info("Embedding model loaded: %s (%d dims)", self.model_name, actual)
            return model

    async def warmup(self) -> bool:
        """Load the model at startup. Never raises; returns success."""
        if not self.enabled:
            logger.info("Embeddings disabled; /ask will use keyword search only")
            return False
        try:
            await anyio.to_thread.run_sync(self.load)
            return True
        except EmbeddingError as exc:
            logger.warning("Embedding warmup failed: %s", exc)
            return False

    # -------------------------------------------------------------- encoding
    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self.load()
        try:
            vectors = model.encode(
                texts,
                batch_size=self.batch_size,
                convert_to_numpy=True,
                # Cosine distance in pgvector assumes unit-length vectors.
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:  # noqa: BLE001 - model runtime errors
            raise EmbeddingError(f"Encoding failed: {exc}") from exc
        return [[float(value) for value in vector] for vector in vectors]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.enabled:
            raise EmbeddingError("Embeddings are disabled (EMBEDDINGS_ENABLED=false)")
        if not texts:
            return []
        return await anyio.to_thread.run_sync(self._encode, texts)

    async def embed_query(self, question: str) -> list[float]:
        vectors = await self.embed_texts([question])
        return vectors[0]
