"""Allow source-explicit deleted provisions without conflating them with omissions.

Revision ID: 20261008_11
Revises: 20261008_10
"""

from alembic import op


revision = "20261008_11"
down_revision = "20261008_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_provisions_source_status", "provisions", type_="check")
    op.create_check_constraint(
        "ck_provisions_source_status", "provisions",
        "source_status IS NULL OR source_status IN ('omitted', 'repealed', 'deleted')",
    )


def downgrade() -> None:
    # Downgrade must not silently discard the more precise historical label.
    op.execute(
        "DO $$ BEGIN IF EXISTS "
        "(SELECT 1 FROM provisions WHERE source_status = 'deleted') "
        "THEN RAISE EXCEPTION 'Cannot downgrade while deleted source statuses exist'; "
        "END IF; END $$"
    )
    op.drop_constraint("ck_provisions_source_status", "provisions", type_="check")
    op.create_check_constraint(
        "ck_provisions_source_status", "provisions",
        "source_status IS NULL OR source_status IN ('omitted', 'repealed')",
    )
