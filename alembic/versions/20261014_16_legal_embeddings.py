"""Add embedding_profiles and legal_chunk_embeddings tables (bge-m3, 1024 dims).

Revision ID: 20261014_16
Revises: 20261013_15

This migration:
1. Ensures the pgvector extension is enabled (CREATE EXTENSION IF NOT EXISTS).
2. Creates ``embedding_profiles`` — one row per (model, dimension, recipe).
3. Creates ``legal_chunk_embeddings`` — one vector(1024) per (chunk, profile),
   with a cosine-distance HNSW index.

Why a separate table (not a column on legal_chunks):
- Different models/dimensions can coexist without an ALTER COLUMN.
- Re-embedding for a new model leaves existing vectors intact.
- The idempotency key (chunk_id, profile_id) prevents duplicates.

Downgrade:
- Drops both tables and their indexes.
- Does NOT disable the pgvector extension (other tables may use it).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261014_16"
down_revision = "20261013_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Enable pgvector (safe to run on a DB that already has it)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. embedding_profiles
    op.create_table(
        "embedding_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("input_recipe", sa.String(100), nullable=False),
        sa.Column("normalize", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_embedding_profiles_name"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_embedding_profiles_name_nonempty"),
        sa.CheckConstraint("dimension > 0", name="ck_embedding_profiles_dimension_positive"),
        sa.CheckConstraint("max_tokens > 0", name="ck_embedding_profiles_max_tokens_positive"),
        sa.CheckConstraint(
            "length(btrim(model_id)) > 0", name="ck_embedding_profiles_model_id_nonempty"
        ),
    )

    # 3. legal_chunk_embeddings
    #    The vector column is created as TEXT first, then cast, because
    #    SQLAlchemy's generic DDL does not know about the pgvector type.
    #    We use raw SQL via op.execute for the column definition.
    op.create_table(
        "legal_chunk_embeddings",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        # placeholder — replaced by ALTER COLUMN below
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("chunk_id", "profile_id"),
        sa.ForeignKeyConstraint(
            ["chunk_id", "revision_id"],
            ["legal_chunks.id", "legal_chunks.revision_id"],
            ondelete="CASCADE",
            name="fk_lce_chunk",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profiles.id"],
            ondelete="RESTRICT",
            name="fk_lce_profile",
        ),
        sa.UniqueConstraint("chunk_id", "profile_id", name="uq_lce_chunk_profile"),
        sa.CheckConstraint(
            "length(btrim(input_sha256)) = 64", name="ck_lce_input_sha256"
        ),
    )

    # Replace the TEXT placeholder with the proper vector(1024) type
    op.execute(
        "ALTER TABLE legal_chunk_embeddings "
        "ALTER COLUMN embedding TYPE vector(1024) USING NULL"
    )
    # Now set NOT NULL
    op.alter_column("legal_chunk_embeddings", "embedding", nullable=False)

    # HNSW index for fast approximate nearest-neighbour cosine search.
    # m=16 / ef_construction=64 are good starting values for ~500–50k vectors.
    # Increase ef_construction to 128 if recall is insufficient after evaluation.
    op.execute(
        "CREATE INDEX ix_lce_embedding_hnsw "
        "ON legal_chunk_embeddings "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # B-tree index for profile-scoped lookups (stats, build progress)
    op.create_index(
        "ix_lce_profile_id",
        "legal_chunk_embeddings",
        ["profile_id"],
    )
    op.create_index(
        "ix_lce_chunk_id",
        "legal_chunk_embeddings",
        ["chunk_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_lce_chunk_id", table_name="legal_chunk_embeddings")
    op.drop_index("ix_lce_profile_id", table_name="legal_chunk_embeddings")
    op.execute("DROP INDEX IF EXISTS ix_lce_embedding_hnsw")
    op.drop_table("legal_chunk_embeddings")
    op.drop_table("embedding_profiles")
    # We intentionally do NOT drop the vector extension; other tables may use it.

