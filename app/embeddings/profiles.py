"""Immutable embedding profiles for the LegalMind corpus.

An ``EmbeddingProfile`` pins every parameter that determines what a vector
means: model identity, dimension, tokenizer, max input tokens, and the text
recipe used to build the embedding input from a chunk.

WHY THIS FILE EXISTS
--------------------
Storing the profile as a named constant (rather than in .env or the DB alone)
means:
- The *codebase* is the single source of truth for which model produced which
  vectors.  A git diff tells you exactly what changed.
- The build script and vector search both import the same object, so input
  recipe and dimension can never silently diverge.
- Switching to a new model is one line change (``ACTIVE_PROFILE = ...``).

TO ADD A NEW MODEL
------------------
1. Define a new constant below (copy BGE_M3 as a template).
2. Change ``ACTIVE_PROFILE`` to point at it.
3. If the dimension changed, write a new Alembic migration.
4. Re-run ``python -m scripts.pipeline.build_legal_embeddings``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """All parameters that define one embedding configuration.

    Every field is immutable so that profile objects can be safely cached and
    compared by value.
    """

    # Human-readable identifier stored in the DB ``embedding_profiles`` table.
    # Must be unique across all profiles ever deployed.
    name: str

    # Hugging Face model path used by sentence-transformers.
    model_id: str

    # Output vector length.  Must match the ``vector(N)`` column in Postgres.
    dimension: int

    # Maximum tokens including special tokens. Oversized inputs are rejected.
    max_tokens: int

    # Short description of how ``chunk_text`` is formatted before encoding.
    # Changing this recipe produces incomparable vectors → increment name.
    input_recipe: str

    # Whether to L2-normalise output vectors. Cosine itself also supports
    # non-normalized nonzero vectors; this is our pinned encoding policy.
    normalize: bool = True


# ---------------------------------------------------------------------------
# Available profiles
# ---------------------------------------------------------------------------

BGE_M3 = EmbeddingProfile(
    name="bge-m3-v1",
    model_id="BAAI/bge-m3",
    dimension=1024,
    max_tokens=8192,
    # We embed: "Act title — label — heading\n\nchunk_text"
    # This mirrors search_text used for keyword search (see legal_chunking.py)
    # but relies on the dense encoder to handle long statutory passages.
    input_recipe="title_label_heading_body",
    normalize=True,
)

# ---------------------------------------------------------------------------
# Active profile — change this line to switch models globally.
# ---------------------------------------------------------------------------

ACTIVE_PROFILE: EmbeddingProfile = BGE_M3


def assert_profile_matches(stored, profile: EmbeddingProfile) -> None:
    """A reused name must match every persisted encoding parameter."""
    fields = ("name", "model_id", "dimension", "max_tokens", "input_recipe", "normalize")
    different = [field for field in fields if getattr(stored, field) != getattr(profile, field)]
    if different:
        raise ValueError(f"Embedding profile {profile.name!r} differs in {', '.join(different)}; "
                         "use a new profile name for a changed configuration")

