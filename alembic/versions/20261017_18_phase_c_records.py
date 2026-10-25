"""Add Phase C answer audit tables only.

Revision ID: 20261017_18
Revises: 20261016_17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "20261017_18"
down_revision = "20261016_17"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table("qa_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("publication_id", pg.UUID(as_uuid=True)),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("model", sa.String(120)),
        sa.Column("reranker_model", sa.String(160)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table("retrieval_hits",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("qa_run_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("provision_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("retrieval_rank", sa.Integer, nullable=False),
        sa.Column("exact_rank", sa.Integer),
        sa.Column("keyword_rank", sa.Integer),
        sa.Column("vector_rank", sa.Integer),
        sa.Column("rrf_rank", sa.Integer),
        sa.Column("rrf_score", sa.Float, nullable=False),
        sa.Column("reranker_rank", sa.Integer),
        sa.Column("reranker_score", sa.Float),
    )
    op.create_index("ix_retrieval_hits_run", "retrieval_hits", ["qa_run_id"])
    op.create_table("answer_claims",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("qa_run_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("evidence_id", sa.String(90), nullable=False),
        sa.UniqueConstraint("qa_run_id", "ordinal", name="uq_answer_claims_run_ordinal"),
    )
    op.create_table("answer_evidence",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("answer_claim_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("answer_claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", sa.String(90), nullable=False),
        sa.Column("chunk_id", pg.UUID(as_uuid=True)),
        sa.Column("provision_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source_span_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("page_index", sa.Integer, nullable=False),
        sa.Column("quote", sa.Text, nullable=False),
    )
    op.create_index("ix_answer_evidence_claim", "answer_evidence", ["answer_claim_id"])


def downgrade():
    op.drop_table("answer_evidence")
    op.drop_table("answer_claims")
    op.drop_table("retrieval_hits")
    op.drop_table("qa_runs")
