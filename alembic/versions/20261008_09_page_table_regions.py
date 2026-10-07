"""Preserve detected page-table rows and columns for visual review.

Revision ID: 20261008_09
Revises: 20261007_08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261008_09"
down_revision = "20261007_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "file_pages",
        sa.Column("table_regions", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("file_pages", "table_regions")
