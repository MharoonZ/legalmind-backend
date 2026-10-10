"""Persist draft parser warnings and per-source-block coverage.

Revision ID: 20261006_06
Revises: 20261005_05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261006_06"
down_revision = "20261005_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corpus_revisions", sa.Column("parser_report", postgresql.JSONB()))
    op.create_table(
        "revision_block_coverage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id", "extraction_id"],
            ["revision_sources.revision_id", "revision_sources.extraction_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "page_id", "extraction_id"],
            ["file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("revision_id", "block_id", name="uq_revision_block_coverage_block"),
        sa.CheckConstraint("status IN ('mapped', 'partial', 'ignored', 'unmapped')", name="ck_revision_block_coverage_status"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_revision_block_coverage_reason"),
    )
    op.create_index("ix_revision_block_coverage_revision_status", "revision_block_coverage", ["revision_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_revision_block_coverage_revision_status", table_name="revision_block_coverage")
    op.drop_table("revision_block_coverage")
    op.drop_column("corpus_revisions", "parser_report")
