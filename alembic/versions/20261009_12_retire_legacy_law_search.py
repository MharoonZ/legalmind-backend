"""Retire the 30-section Contract Act search tables.

Revision ID: 20261009_12
Revises: 20261008_11

The source/section/passage records are archived separately before applying this
migration. The full-PDF corpus and uploaded-contract tables are unaffected.
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.core.config import get_settings


revision = "20261009_12"
down_revision = "20261008_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # RESTRICT is intentional: fail if anything outside the legacy trio depends
    # on them. Never cascade into the new PDF corpus or contract records.
    op.drop_table("law_passages")
    op.drop_table("law_sections")
    op.drop_table("law_sources")


def downgrade() -> None:
    # Schema-only restoration. The local JSON archive holds the retired rows.
    op.create_table(
        "law_sources",
        sa.Column("id", sa.String(72), primary_key=True),
        sa.Column("law_name", sa.String(200), nullable=False),
        sa.Column("jurisdiction", sa.String(100), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("pdf_sha256", sa.String(64), nullable=False),
        sa.Column("corpus_sha256", sa.String(64), nullable=False),
        sa.Column("pdf_page_count", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False),
        sa.Column("reviewed_by", sa.String(200)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("review_status IN ('needs_review', 'approved', 'rejected')", name="ck_law_sources_review_status"),
        sa.CheckConstraint("review_status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="ck_law_sources_approval_recorded"),
        sa.UniqueConstraint("corpus_sha256", name="uq_law_sources_corpus_sha256"),
    )
    op.create_table(
        "law_sections",
        sa.Column("id", sa.String(76), primary_key=True),
        sa.Column("source_id", sa.String(72), sa.ForeignKey("law_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("section_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("pdf_page_start", sa.Integer(), nullable=False),
        sa.Column("pdf_page_end", sa.Integer(), nullable=False),
        sa.CheckConstraint("section_number BETWEEN 1 AND 30", name="ck_law_sections_sprint_range"),
        sa.CheckConstraint("pdf_page_start > 0 AND pdf_page_end >= pdf_page_start", name="ck_law_sections_pages"),
        sa.UniqueConstraint("source_id", "section_number", name="uq_law_sections_source_number"),
    )
    op.create_index("ix_law_sections_source_id", "law_sections", ["source_id"])
    op.create_table(
        "law_passages",
        sa.Column("evidence_id", sa.String(67), primary_key=True),
        sa.Column("section_id", sa.String(76), sa.ForeignKey("law_sections.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("search_vector", TSVECTOR(), sa.Computed("to_tsvector('english'::regconfig, search_text)", persisted=True), nullable=False),
        sa.Column("embedding", Vector(get_settings().embedding_dim)),
        sa.Column("embedding_model", sa.String(100)),
        sa.CheckConstraint("page_number > 0", name="ck_law_passages_page_positive"),
        sa.CheckConstraint("(embedding IS NULL AND embedding_model IS NULL) OR (embedding IS NOT NULL AND embedding_model IS NOT NULL)", name="ck_law_passages_embedding_pair"),
        sa.UniqueConstraint("section_id", "page_number", name="uq_law_passages_section_page"),
    )
    op.create_index("ix_law_passages_section_id", "law_passages", ["section_id"])
    op.create_index("ix_law_passages_search_vector", "law_passages", ["search_vector"], postgresql_using="gin")
    if get_settings().embedding_dim <= 2000:
        op.execute("CREATE INDEX ix_law_passages_embedding_hnsw ON law_passages USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
