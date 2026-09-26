"""Create documents and document_versions tables for uploaded contracts.

Revision ID: 20260926_03
Revises: 20260920_02

Sprint 2 (Abdullah, Week 1): the upload stage only. Kept separate from the
law_sources/law_sections/law_passages tables per Sprint 2 shared rule 6 - a
user's uploaded contract is evidence to check against the law, never text
the law-corpus search endpoint could return.

Clause/citation tables (Haroon) and summary tables (Week 2 AI) are not part
of this migration. They should reference `document_versions.id` once ready.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260926_03"
down_revision = "20260920_02"
branch_labels = None
depends_on = None

_STATUSES = (
    "uploaded",
    "extracting",
    "extracted",
    "extraction_failed",
    "summarizing",
    "summarized",
    "summary_failed",
)


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("pdf_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        sa.CheckConstraint("page_count > 0", name="ck_documents_page_count_positive"),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(48),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("question", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_document_versions_status",
        ),
        sa.UniqueConstraint(
            "document_id", "version_number", name="uq_document_versions_number"
        ),
    )
    op.create_index(
        "ix_document_versions_document_id", "document_versions", ["document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_table("documents")
