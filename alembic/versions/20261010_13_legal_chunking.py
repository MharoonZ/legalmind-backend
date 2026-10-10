"""Add Phase B legal-aware chunk tables (chunk_sets, legal_chunks, legal_chunk_provisions).

Revision ID: 20261010_13
Revises: 20261009_12

Chunking input is an approved ``corpus_revisions`` row, pinned by
``(revision_id, content_sha256)``. These tables only hold Abdullah's derived
chunk data; they never duplicate or rewrite Phase A provenance. A chunk's
exact source text/page/block mapping is always reached by following
``legal_chunk_provisions.provision_id`` to that provision's existing
``provision_spans`` rows, never stored again here.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261010_13"
down_revision = "20261009_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chunk_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("chunker_version", sa.String(50), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("chunk_report", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["revision_id"], ["corpus_revisions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "revision_id", name="uq_chunk_sets_id_revision"),
        sa.UniqueConstraint(
            "revision_id", "content_sha256", "chunker_version", "config_sha256",
            name="uq_chunk_sets_build_fingerprint",
        ),
        sa.CheckConstraint("length(btrim(content_sha256)) = 64", name="ck_chunk_sets_content_hash"),
        sa.CheckConstraint("length(btrim(chunker_version)) > 0", name="ck_chunk_sets_chunker_version"),
        sa.CheckConstraint("length(btrim(config_sha256)) = 64", name="ck_chunk_sets_config_hash"),
    )
    op.create_index("ix_chunk_sets_revision_id", "chunk_sets", ["revision_id"])

    op.create_table(
        "legal_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chunk_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_provision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("heading", sa.Text()),
        sa.Column("reference_path", sa.Text(), nullable=False),
        sa.Column("chunk_key", sa.Text(), nullable=False),
        sa.Column("sequence_in_root", sa.Integer(), nullable=False),
        sa.Column("root_total_chunks", sa.Integer(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("chunk_text_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["chunk_set_id", "revision_id"], ["chunk_sets.id", "chunk_sets.revision_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["root_provision_id", "revision_id"], ["provisions.id", "provisions.revision_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "chunk_set_id", "revision_id", name="uq_legal_chunks_id_set_revision"),
        sa.UniqueConstraint("id", "revision_id", name="uq_legal_chunks_id_revision"),
        sa.UniqueConstraint("chunk_set_id", "ordinal", name="uq_legal_chunks_set_ordinal"),
        sa.UniqueConstraint("chunk_set_id", "chunk_key", name="uq_legal_chunks_set_key"),
        sa.CheckConstraint(
            "kind IN ('preamble', 'section', 'rule', 'article', 'schedule')",
            name="ck_legal_chunks_kind",
        ),
        sa.CheckConstraint("ordinal > 0", name="ck_legal_chunks_ordinal"),
        sa.CheckConstraint(
            "sequence_in_root > 0 AND sequence_in_root <= root_total_chunks",
            name="ck_legal_chunks_sequence",
        ),
        sa.CheckConstraint("char_count > 0", name="ck_legal_chunks_char_count"),
        sa.CheckConstraint("length(btrim(prefix)) > 0", name="ck_legal_chunks_prefix_nonempty"),
        sa.CheckConstraint("length(btrim(chunk_text)) > 0", name="ck_legal_chunks_text_nonempty"),
        sa.CheckConstraint("length(btrim(chunk_key)) > 0", name="ck_legal_chunks_key_nonempty"),
        sa.CheckConstraint("length(btrim(chunk_text_sha256)) = 64", name="ck_legal_chunks_text_hash"),
    )
    op.create_index("ix_legal_chunks_chunk_set_id", "legal_chunks", ["chunk_set_id"])
    op.create_index("ix_legal_chunks_root_provision_id", "legal_chunks", ["root_provision_id"])

    op.create_table(
        "legal_chunk_provisions",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id", "provision_id"),
        sa.ForeignKeyConstraint(
            ["chunk_id", "revision_id"], ["legal_chunks.id", "legal_chunks.revision_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provision_id", "revision_id"], ["provisions.id", "provisions.revision_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("chunk_id", "ordinal", name="uq_legal_chunk_provisions_chunk_ordinal"),
        sa.CheckConstraint("ordinal > 0", name="ck_legal_chunk_provisions_ordinal"),
    )
    op.create_index("ix_legal_chunk_provisions_provision_id", "legal_chunk_provisions", ["provision_id"])


def downgrade() -> None:
    op.drop_index("ix_legal_chunk_provisions_provision_id", table_name="legal_chunk_provisions")
    op.drop_table("legal_chunk_provisions")
    op.drop_index("ix_legal_chunks_root_provision_id", table_name="legal_chunks")
    op.drop_index("ix_legal_chunks_chunk_set_id", table_name="legal_chunks")
    op.drop_table("legal_chunks")
    op.drop_index("ix_chunk_sets_revision_id", table_name="chunk_sets")
    op.drop_table("chunk_sets")
