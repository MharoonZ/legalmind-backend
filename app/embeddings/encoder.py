"""BAAI/bge-m3 encoder — model loading and batch text encoding.

Design decisions
----------------
* The model is loaded **once** via ``get_encoder()`` (module-level singleton
  guarded by a lock) and reused for all subsequent calls in the same process.
  This avoids the ~5 second model load overhead on every batch.

* Encoding is done through ``sentence-transformers`` which handles tokenization,
  batching, padding, and normalisation transparently.

* Inputs are checked with the actual tokenizer, including special tokens.
  Oversized inputs are rejected rather than silently losing legal text.
  The historical v1 character ceiling remains a rejection boundary; inputs
  below it have exactly the same recipe and hashes as existing vectors.

* Encoding is deliberately synchronous (sentence-transformers runs on CPU/GPU
  via PyTorch; asyncio's thread pool is used in the async wrapper below).

Changing the model
------------------
Update ``ACTIVE_PROFILE`` in ``profiles.py``.  Do **not** change this file
unless the loading or encoding logic itself needs to change.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from app.embeddings.profiles import EmbeddingProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton model cache (one per complete immutable profile)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# A SentenceTransformer instance is shared by synchronous and asynchronous
# callers. Keep CPU/GPU forward passes serialized in a process.
_inference_lock = threading.Lock()
_models: dict[EmbeddingProfile, "SentenceTransformer"] = {}


class EmbeddingInputError(ValueError):
    """The complete input cannot be encoded under the pinned profile."""


def validate_vector(vector: list[float], profile: EmbeddingProfile) -> None:
    if len(vector) != profile.dimension or not all(math.isfinite(x) for x in vector):
        raise ValueError(f"Expected {profile.dimension} finite vector components")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0 or (profile.normalize and abs(norm - 1.0) > 0.001):
        raise ValueError("Embedding is zero or is not normalized as configured")


def check_token_lengths(texts: list[str], tokenizer, max_tokens: int) -> list[int]:
    lengths = [len(tokenizer.encode(t, add_special_tokens=True, truncation=False)) for t in texts]
    if any(n > max_tokens for n in lengths):
        raise EmbeddingInputError(
            f"Input requires {max(lengths)} tokens; profile allows {max_tokens}. "
            "Split the source into a new chunk set; no vector was written."
        )
    return lengths


@lru_cache(maxsize=4)
def get_tokenizer(profile: EmbeddingProfile):
    """Audit tokens without downloading/loading model weights."""
    from transformers import AutoTokenizer
    try:
        return AutoTokenizer.from_pretrained(profile.model_id, trust_remote_code=False, local_files_only=True)
    except OSError:
        return AutoTokenizer.from_pretrained(profile.model_id, trust_remote_code=False)


def get_encoder(profile: EmbeddingProfile) -> "SentenceTransformer":
    """Return the SentenceTransformer for ``profile``, loading it once.

    Thread-safe: the first call loads the model; subsequent calls return the
    cached instance immediately.

    Raises
    ------
    ImportError
        If ``sentence-transformers`` is not installed.
    """
    if profile not in _models:
        with _lock:
            if profile not in _models:
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
                try:
                    model = SentenceTransformer(profile.model_id, trust_remote_code=False,
                                                local_files_only=True)
                except OSError:
                    model = SentenceTransformer(profile.model_id, trust_remote_code=False)
                if model.get_sentence_embedding_dimension() != profile.dimension:
                    raise ValueError("Loaded model dimension disagrees with embedding profile")
                if model.max_seq_length < profile.max_tokens:
                    raise ValueError("Loaded model token limit is smaller than the stored profile")
                model.max_seq_length = profile.max_tokens
                _models[profile] = model
                logger.info("Model %s loaded (dim=%d).", profile.model_id, profile.dimension)
    return _models[profile]


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

    Preserves the v1 recipe for all previously accepted complete inputs.
    Rejects text that the old implementation would have truncated.
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
        raise EmbeddingInputError(
            f"Input has {len(text)} characters, beyond the legacy recipe ceiling "
            f"of {char_budget}. Re-chunk instead of truncating."
        )
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
    check_token_lengths(texts, model.tokenizer, profile.max_tokens)
    logger.info("Encoding %d texts with %s …", len(texts), profile.model_id)

    with _inference_lock:
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=profile.normalize,
            show_progress_bar=False,
        )
    # sentence-transformers returns a numpy array; convert to plain Python lists
    # so the result is JSON-serialisable and works with pgvector's list input.
    vectors = [vec.tolist() for vec in embeddings]
    if len(vectors) != len(texts):
        raise ValueError("Encoder returned an unexpected number of vectors")
    for vector in vectors:
        validate_vector(vector, profile)
    return vectors


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
    from app.services.model_runtime import runtime
    return await runtime.run(lambda stop: encode_texts(texts, profile, batch_size=batch_size),
                             stage="query_embedding" if len(texts) == 1 else "embedding_batch")

