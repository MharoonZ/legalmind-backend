"""BAAI/bge-m3 encoder — model loading and batch text encoding.

Design decisions
----------------
* The model is loaded **once** via ``get_encoder()`` (module-level singleton
  guarded by a lock) and reused for all subsequent calls in the same process.
  This avoids the ~5 second model load overhead on every batch.

* Encoding is done through ``sentence-transformers`` which handles tokenization,
  batching, padding, and normalisation transparently.

* Text is pre-truncated in *characters* before being passed to the model.
  Because bge-m3 uses a byte-pair / word-piece tokenizer, one character ≠ one
  token.  The encoder still hard-truncates at ``max_tokens`` internally, but
  pre-truncating at ``max_tokens * 3`` characters (a conservative ratio for
  English legal prose) avoids surprising silent losses.

* Encoding is deliberately synchronous (sentence-transformers runs on CPU/GPU
  via PyTorch; asyncio's thread pool is used in the async wrapper below).

Changing the model
------------------
Update ``ACTIVE_PROFILE`` in ``profiles.py``.  Do **not** change this file
unless the loading or encoding logic itself needs to change.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from app.embeddings.profiles import EmbeddingProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton model cache (one per profile, keyed by profile.name)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_models: dict[str, "SentenceTransformer"] = {}


def get_encoder(profile: EmbeddingProfile) -> "SentenceTransformer":
    """Return the SentenceTransformer for ``profile``, loading it once.

    Thread-safe: the first call loads the model; subsequent calls return the
    cached instance immediately.

    Raises
    ------
    ImportError
        If ``sentence-transformers`` is not installed.
    """
    if profile.name not in _models:
        with _lock:
            if profile.name not in _models:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise ImportError(
                        "sentence-transformers is required for embeddings. "
                        "Install it: pip install sentence-transformers"
                    ) from exc
                logger.info(
                    "Loading embedding model %s (profile=%s) …",
                    profile.model_id,
                    profile.name,
                )
                model = SentenceTransformer(
                    profile.model_id,
                    trust_remote_code=True,   # bge-m3 uses custom pooling code
                )
                _models[profile.name] = model
                logger.info("Model %s loaded (dim=%d).", profile.model_id, profile.dimension)
    return _models[profile.name]


# ---------------------------------------------------------------------------
# Text input recipe
# ---------------------------------------------------------------------------

def build_embedding_input(
    canonical_title: str,
    label: str | None,
    heading: str | None,
    chunk_text: str,
    profile: EmbeddingProfile,
) -> str:
    """Assemble the text fed to the encoder for one passage chunk.

    The recipe mirrors the ``search_text`` used for keyword search (Act title +
    label + heading + body) so both retrieval modes see the same context.

    Pre-truncates at a character level to avoid silent token-level losses
    inside the model.  The resulting string is the canonical input; its SHA-256
    is stored alongside the vector for idempotency checks.
    """
    parts = [canonical_title]
    if label:
        parts.append(label)
    if heading:
        parts.append(heading)
    prefix = " — ".join(parts)
    text = f"{prefix}\n\n{chunk_text}"

    # Conservative pre-truncation: English legal text averages ~3 chars/token
    char_budget = profile.max_tokens * 3
    if len(text) > char_budget:
        logger.warning(
            "Chunk input truncated from %d to %d chars (profile=%s).",
            len(text), char_budget, profile.name,
        )
        text = text[:char_budget]
    return text


def input_sha256(text: str) -> str:
    """SHA-256 of the embedding input text (for idempotency checks)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Batch encoding (sync + async wrappers)
# ---------------------------------------------------------------------------

def encode_texts(
    texts: list[str],
    profile: EmbeddingProfile,
    batch_size: int = 16,
) -> list[list[float]]:
    """Encode a list of texts with the given profile's model.

    Parameters
    ----------
    texts:
        Pre-assembled embedding inputs (output of ``build_embedding_input``).
    profile:
        Must match the model being used; ``profile.normalize`` controls
        whether vectors are L2-normalised.
    batch_size:
        Number of passages encoded per forward pass.  16 is a safe default
        on CPU; increase to 32-64 on a GPU.

    Returns
    -------
    list[list[float]]
        One vector per input text, in the same order.
    """
    if not texts:
        return []

    model = get_encoder(profile)
    logger.info("Encoding %d texts with %s …", len(texts), profile.model_id)

    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=profile.normalize,
        show_progress_bar=False,
    )
    # sentence-transformers returns a numpy array; convert to plain Python lists
    # so the result is JSON-serialisable and works with pgvector's list input.
    return [vec.tolist() for vec in embeddings]


async def encode_texts_async(
    texts: list[str],
    profile: EmbeddingProfile,
    batch_size: int = 16,
) -> list[list[float]]:
    """Async wrapper around ``encode_texts``.

    Runs the CPU/GPU-bound encoding in a thread-pool executor so it does not
    block the asyncio event loop (important when called from a FastAPI endpoint
    or an async script).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: encode_texts(texts, profile, batch_size=batch_size),
    )

