"""Link source-note records to exact printed amendment/footnote markers.

Revision ID: 20261007_07
Revises: 20261006_06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261007_07"
down_revision = "20261006_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_note_marker_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note_provision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_provision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("marker_label", sa.String(20), nullable=False),
        sa.Column("extraction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_char_start", sa.Integer(), nullable=False),
        sa.Column("block_char_end", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["note_provision_id", "revision_id"],
            ["provisions.id", "provisions.revision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_provision_id", "revision_id"],
            ["provisions.id", "provisions.revision_id"],
            ondelete="RESTRICT",
        ),
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
        sa.UniqueConstraint(
            "revision_id", "block_id", "block_char_start", "block_char_end",
            name="uq_source_note_marker_links_marker_source",
        ),
        sa.CheckConstraint(
            "block_char_start >= 0 AND block_char_end > block_char_start",
            name="ck_source_note_marker_links_block_range",
        ),
        sa.CheckConstraint("marker_label ~ '^[0-9]+$'", name="ck_source_note_marker_links_numeric_label"),
        sa.CheckConstraint(
            "target_provision_id IS NULL OR target_provision_id <> note_provision_id",
            name="ck_source_note_marker_links_distinct_nodes",
        ),
    )
    op.create_index(
        "ix_source_note_marker_links_page", "source_note_marker_links", ["revision_id", "page_id"],
    )
    op.create_index(
        "ix_source_note_marker_links_note", "source_note_marker_links", ["note_provision_id"],
    )
    op.create_index(
        "ix_source_note_marker_links_target", "source_note_marker_links", ["target_provision_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_note_marker_links_target", table_name="source_note_marker_links")
    op.drop_index("ix_source_note_marker_links_note", table_name="source_note_marker_links")
    op.drop_index("ix_source_note_marker_links_page", table_name="source_note_marker_links")
    op.drop_table("source_note_marker_links")
