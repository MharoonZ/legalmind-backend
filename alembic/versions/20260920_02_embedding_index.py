"""Match the embedding column to the configured model and add an HNSW index.

Revision ID: 20260920_02
Revises: 20260919_01

The first migration reserved `vector(3072)`. Two things made that impractical:

* pgvector cannot build an HNSW or IVFFlat index above 2000 dimensions, so
  every search would be an exact scan;
* the chosen embedding model (sentence-transformers/all-MiniLM-L6-v2) emits
  384 dimensions, and a vector column must match its model exactly.

This migration resizes the column to `EMBEDDING_DIM` and creates the cosine
HNSW index. It refuses to run if embeddings already exist, because silently
discarding stored vectors would break every citation that depends on them.
"""

from alembic import op
import sqlalchemy as sa

from app.core.config import get_settings

revision = "20260920_02"
down_revision = "20260919_01"
branch_labels = None
depends_on = None

PREVIOUS_DIM = 3072
HNSW_MAX_DIM = 2000


def _target_dim() -> int:
    return get_settings().embedding_dim


def upgrade() -> None:
    dim = _target_dim()
    bind = op.get_bind()

    existing = bind.execute(
        sa.text("SELECT count(*) FROM law_passages WHERE embedding IS NOT NULL")
    ).scalar_one()
    if existing and dim != PREVIOUS_DIM:
        raise RuntimeError(
            f"{existing} passages already have embeddings. Clear them first "
            "(UPDATE law_passages SET embedding = NULL, embedding_model = NULL) "
            "and rebuild with scripts/build_embeddings.py."
        )

    # Safe because the column is empty at this point.
    op.execute(
        f"ALTER TABLE law_passages "
        f"ALTER COLUMN embedding TYPE vector({dim}) USING NULL"
    )

    if dim <= HNSW_MAX_DIM:
        # Cosine distance, matching the normalised vectors we store and the
        # `<=>` operator used by LawRepository.vector_search.
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_law_passages_embedding_hnsw "
            "ON law_passages USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_law_passages_embedding_hnsw")
    op.execute(
        f"ALTER TABLE law_passages "
        f"ALTER COLUMN embedding TYPE vector({PREVIOUS_DIM}) USING NULL"
    )
