"""Record exact ordered source fragments without rewriting historical chunks.

Revision ID: 20261013_15
Revises: 20261010_14

The v1 membership key allowed a provision only once per chunk. A parent can
appear before and after its children in the PDF, so v2 needs one link row per
source fragment. Existing rows remain valid with nullable fragment columns.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261013_15"
down_revision = "20261010_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("legal_chunk_provisions", sa.Column("source_span_id", postgresql.UUID(as_uuid=True)))
    op.add_column("legal_chunk_provisions", sa.Column("provision_char_start", sa.Integer()))
    op.add_column("legal_chunk_provisions", sa.Column("provision_char_end", sa.Integer()))
    op.add_column("legal_chunk_provisions", sa.Column("chunk_char_start", sa.Integer()))
    op.add_column("legal_chunk_provisions", sa.Column("chunk_char_end", sa.Integer()))
    op.drop_constraint("legal_chunk_provisions_pkey", "legal_chunk_provisions", type_="primary")
    op.create_primary_key("legal_chunk_provisions_pkey", "legal_chunk_provisions", ["chunk_id", "ordinal"])
    op.create_foreign_key(
        "fk_legal_chunk_provisions_source_span", "legal_chunk_provisions", "provision_spans",
        ["source_span_id", "provision_id", "revision_id"],
        ["id", "provision_id", "revision_id"], ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_legal_chunk_provisions_fragment", "legal_chunk_provisions",
        "(source_span_id IS NULL AND provision_char_start IS NULL AND provision_char_end IS NULL "
        "AND chunk_char_start IS NULL AND chunk_char_end IS NULL) OR "
        "(source_span_id IS NOT NULL AND provision_char_start >= 0 "
        "AND provision_char_end > provision_char_start AND chunk_char_start >= 0 "
        "AND chunk_char_end > chunk_char_start)",
    )
    op.create_index("ix_legal_chunk_provisions_source_span_id", "legal_chunk_provisions", ["source_span_id"])


def downgrade() -> None:
    # Refuse to discard v2 provenance; rollback must first remove only v2
    # chunk sets in a deliberate, separately backed-up operation.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM legal_chunk_provisions WHERE source_span_id IS NOT NULL) ")).scalar():
        raise RuntimeError("Cannot downgrade while source-fragment links exist; preserve/backup v2 chunk sets first.")
    op.drop_index("ix_legal_chunk_provisions_source_span_id", table_name="legal_chunk_provisions")
    op.drop_constraint("ck_legal_chunk_provisions_fragment", "legal_chunk_provisions", type_="check")
    op.drop_constraint("fk_legal_chunk_provisions_source_span", "legal_chunk_provisions", type_="foreignkey")
    op.drop_constraint("legal_chunk_provisions_pkey", "legal_chunk_provisions", type_="primary")
    op.create_primary_key("legal_chunk_provisions_pkey", "legal_chunk_provisions", ["chunk_id", "provision_id"])
    for name in ("chunk_char_end", "chunk_char_start", "provision_char_end", "provision_char_start", "source_span_id"):
        op.drop_column("legal_chunk_provisions", name)
