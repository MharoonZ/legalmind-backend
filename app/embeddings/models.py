"""SQLAlchemy ORM models for the embedding subsystem.

Two tables
----------
embedding_profiles
    One row per (model, dimension, input-recipe) combination.  Immutable once
    created; never mutated in place.  A new model → new row.

legal_chunk_embeddings
    One vector per (chunk, profile).  The embedding column is typed
    ``Vector(1024)`` for bge-m3.  If a future model uses a different
    dimension, add a new column (e.g. ``embedding_768``) in a separate
    migration rather than altering this one.

pgvector requirement
--------------------
The ``vector`` extension must be enabled in PostgreSQL before running the
migration.  The ``pgvector`` Python package is already in requirements.txt
(it was added for historical Alembic compatibility).  The migration
``20261014_16`` enables the extension and creates both tables.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from app.db.base import Base


class EmbeddingProfileModel(Base):
    """Immutable record of one embedding model configuration.

    The application never updates a row; a new model/recipe gets a new row.
    Old rows are kept so historical vectors remain interpretable.
    """

    __tablename__ = "embedding_profiles"
    __table_args__ = (
        UniqueConstraint("name", name="uq_embedding_profiles_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_embedding_profiles_name_nonempty"),
        CheckConstraint("dimension > 0", name="ck_embedding_profiles_dimension_positive"),
        CheckConstraint("max_tokens > 0", name="ck_embedding_profiles_max_tokens_positive"),
        CheckConstraint("length(btrim(model_id)) > 0", name="ck_embedding_profiles_model_id_nonempty"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    # e.g. "bge-m3-v1"  — must match EmbeddingProfile.name in profiles.py
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # e.g. "BAAI/bge-m3"
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Output vector length, e.g. 1024
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    # Tokenizer context window, e.g. 8192
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # Text assembly recipe identifier, e.g. "title_label_heading_body"
    input_recipe: Mapped[str] = mapped_column(String(100), nullable=False)
    # Whether vectors are L2-normalised (True → cosine via <=> is valid)
    normalize: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LegalChunkEmbedding(Base):
    """One embedding vector for one (chunk, profile) pair.

    Idempotency key: ``(chunk_id, profile_id)``.
    The ``input_sha256`` lets the build script skip chunks whose input text
    has not changed even if the chunk was somehow processed before under the
    same profile.
    """

    __tablename__ = "legal_chunk_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_id", "revision_id"],
            ["legal_chunks.id", "legal_chunks.revision_id"],
            ondelete="CASCADE",
            name="fk_lce_chunk",
        ),
        ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profiles.id"],
            ondelete="RESTRICT",
            name="fk_lce_profile",
        ),
        UniqueConstraint("chunk_id", "profile_id", name="uq_lce_chunk_profile"),
        CheckConstraint(
            "length(btrim(input_sha256)) = 64",
            name="ck_lce_input_sha256",
        ),
    )

    # Composite PK mirrors legal_chunks composite FKs
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # Kept for the composite FK into legal_chunks
    revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # SHA-256 of the assembled embedding input text (for idempotency)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # The actual dense vector — dimension must match embedding_profiles.dimension
    # bge-m3 outputs 1024 dimensions
    embedding: Mapped[list] = mapped_column(Vector(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

