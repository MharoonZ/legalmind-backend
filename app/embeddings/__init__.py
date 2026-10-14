"""Legal-corpus embedding subsystem.

This package holds all embedding-related code: model profiles, the encoder
(model loading + text encoding), the embedding repository (DB persistence
and vector search), and helper utilities.

HOW TO CHANGE THE MODEL IN FUTURE
----------------------------------
1. Add a new profile constant in ``profiles.py`` (e.g. ``BGE_M3_V2``).
   Give it a unique ``name``, correct ``dimension``, and updated
   ``model_id`` / ``max_tokens``.
2. Point ``ACTIVE_PROFILE`` at the new constant.
3. If the *dimension* changed (e.g. 1024 → 768) write a new Alembic
   migration that adds a matching ``vector(N)`` column and HNSW index to
   ``legal_chunk_embeddings``. Same-dimension model upgrades need no schema
   change.
4. Run ``python -m scripts.build_legal_embeddings`` — it is fully idempotent
   and will only encode chunks that do not yet have a vector for the new
   profile; old vectors remain intact for rollback.

FOLDER LAYOUT
-------------
app/embeddings/
    __init__.py      – this file (public re-exports + overview)
    profiles.py      – immutable EmbeddingProfile dataclass + ACTIVE_PROFILE
    encoder.py       – BAAI/bge-m3 model loading and batch encoding
    repository.py    – DB persistence, idempotent upsert, vector search
"""

from app.embeddings.profiles import ACTIVE_PROFILE, EmbeddingProfile  # noqa: F401

