"""Store revision-scoped, exact-source cross-instrument proposals.

Revision ID: 20261007_08
Revises: 20261007_07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261007_08"
down_revision = "20261007_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_legal_versions_id_instrument", "legal_versions", ["id", "instrument_id"],
    )
    op.create_unique_constraint(
        "uq_provision_spans_id_provision_revision", "provision_spans",
        ["id", "provision_id", "revision_id"],
    )
    op.create_table(
        "instrument_relation_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_provision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_span_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_char_start", sa.Integer(), nullable=False),
        sa.Column("source_char_end", sa.Integer(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("target_title", sa.Text(), nullable=False),
        sa.Column("target_reference", sa.Text()),
        sa.Column("candidate_instrument_id", postgresql.UUID(as_uuid=True)),
        sa.Column("candidate_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("relation_kind", sa.String(20), nullable=False),
        sa.Column("link_basis", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["revision_id"], ["corpus_revisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_provision_id", "revision_id"],
            ["provisions.id", "provisions.revision_id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_span_id", "source_provision_id", "revision_id"],
            ["provision_spans.id", "provision_spans.provision_id", "provision_spans.revision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_instrument_id"], ["legal_instruments.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_version_id", "candidate_instrument_id"],
            ["legal_versions.id", "legal_versions.instrument_id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "revision_id", "evidence_span_id", "source_char_start", "source_char_end", "relation_kind",
            name="uq_instrument_relation_candidates_evidence",
        ),
        sa.CheckConstraint(
            "source_char_start >= 0 AND source_char_end > source_char_start",
            name="ck_instrument_relation_candidates_range",
        ),
        sa.CheckConstraint(
            "relation_kind IN ('cites', 'extends', 'amends', 'repeals')",
            name="ck_instrument_relation_candidates_kind",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'unresolved')",
            name="ck_instrument_relation_candidates_status",
        ),
        sa.CheckConstraint(
            "link_basis IN ('explicit_title', 'nearby_reference', 'defined_short_citation')",
            name="ck_instrument_relation_candidates_basis",
        ),
        sa.CheckConstraint(
            "candidate_version_id IS NULL OR candidate_instrument_id IS NOT NULL",
            name="ck_instrument_relation_candidates_version_target",
        ),
        sa.CheckConstraint(
            "length(btrim(target_title)) > 0 AND length(evidence_text) > 0",
            name="ck_instrument_relation_candidates_text",
        ),
    )
    op.create_index(
        "ix_instrument_relation_candidates_revision", "instrument_relation_candidates", ["revision_id"],
    )
    op.create_index(
        "ix_instrument_relation_candidates_target", "instrument_relation_candidates", ["candidate_instrument_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_instrument_relation_candidates_target", table_name="instrument_relation_candidates")
    op.drop_index("ix_instrument_relation_candidates_revision", table_name="instrument_relation_candidates")
    op.drop_table("instrument_relation_candidates")
    op.drop_constraint("uq_provision_spans_id_provision_revision", "provision_spans", type_="unique")
    op.drop_constraint("uq_legal_versions_id_instrument", "legal_versions", type_="unique")
