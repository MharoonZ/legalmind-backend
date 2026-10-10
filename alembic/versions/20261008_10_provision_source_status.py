"""Record only source-explicit omitted/repealed provision labels.

Revision ID: 20261008_10
Revises: 20261008_09
"""

from alembic import op
import sqlalchemy as sa


revision = "20261008_10"
down_revision = "20261008_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provisions", sa.Column("source_status", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_provisions_source_status", "provisions",
        "source_status IS NULL OR source_status IN ('omitted', 'repealed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_provisions_source_status", "provisions", type_="check")
    op.drop_column("provisions", "source_status")
