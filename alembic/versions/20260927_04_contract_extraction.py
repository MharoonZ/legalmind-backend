"""Add versioned contract pages, clauses, and page provenance.

Revision ID: 20260927_04
Revises: 20260926_03

Sprint 2 (Haroon): extracted contract text stays separate from the law corpus.
Every clause is scoped to an immutable document version, and every character
range used as evidence can be mapped back to one or more PDF pages.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_04"
down_revision = "20260926_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_versions", sa.Column("extraction_engine", sa.String(50))
    )
    op.add_column(
        "document_versions", sa.Column("extraction_engine_version", sa.String(50))
    )
    op.add_column(
        "document_versions", sa.Column("extracted_text_sha256", sa.String(64))
    )
    op.add_column(
        "document_versions", sa.Column("extraction_started_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "document_versions", sa.Column("extraction_completed_at", sa.DateTime(timezone=True))
    )

    op.create_table(
        "contract_pages",
        sa.Column(
            "document_version_id",
            sa.String(48),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("document_version_id", "page_number"),
        sa.CheckConstraint("page_number > 0", name="ck_contract_pages_page_positive"),
    )

    op.create_table(
        "contract_clauses",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(48),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(100)),
        sa.Column("heading", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_contract_clauses_ordinal_positive"),
        sa.CheckConstraint(
            "page_start > 0 AND page_end >= page_start",
            name="ck_contract_clauses_pages",
        ),
        sa.UniqueConstraint(
            "document_version_id", "ordinal", name="uq_contract_clauses_version_ordinal"
        ),
        sa.UniqueConstraint(
            "id", "document_version_id", name="uq_contract_clauses_id_version"
        ),
    )
    op.create_index(
        "ix_contract_clauses_version_id", "contract_clauses", ["document_version_id"]
    )

    op.create_table(
        "contract_clause_spans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("clause_id", sa.String(48), nullable=False),
        sa.Column("document_version_id", sa.String(48), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("clause_char_start", sa.Integer(), nullable=False),
        sa.Column("clause_char_end", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["clause_id", "document_version_id"],
            ["contract_clauses.id", "contract_clauses.document_version_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "page_number"],
            ["contract_pages.document_version_id", "contract_pages.page_number"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "page_number > 0", name="ck_contract_clause_spans_page_positive"
        ),
        sa.CheckConstraint(
            "clause_char_start >= 0 AND clause_char_end > clause_char_start",
            name="ck_contract_clause_spans_chars",
        ),
        sa.UniqueConstraint(
            "clause_id",
            "page_number",
            "clause_char_start",
            "clause_char_end",
            name="uq_contract_clause_spans_range",
        ),
    )
    op.create_index(
        "ix_contract_clause_spans_clause_id", "contract_clause_spans", ["clause_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_contract_clause_spans_clause_id", table_name="contract_clause_spans")
    op.drop_table("contract_clause_spans")
    op.drop_index("ix_contract_clauses_version_id", table_name="contract_clauses")
    op.drop_table("contract_clauses")
    op.drop_table("contract_pages")
    op.drop_column("document_versions", "extraction_completed_at")
    op.drop_column("document_versions", "extraction_started_at")
    op.drop_column("document_versions", "extracted_text_sha256")
    op.drop_column("document_versions", "extraction_engine_version")
    op.drop_column("document_versions", "extraction_engine")
