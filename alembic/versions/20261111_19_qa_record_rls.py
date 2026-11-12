"""Keep optional QA records inaccessible to direct Supabase client roles."""

from alembic import op

revision = "20261111_19"
down_revision = "20261017_18"
branch_labels = None
depends_on = None

TABLES = ("qa_runs", "retrieval_hits", "answer_claims", "answer_evidence")


def upgrade():
    # Without policies, RLS defaults to deny for anon/authenticated even
    # where Supabase default grants exist. The server-side writer is separate.
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade():
    # Reverting code can leave this migration in place. Never expose records
    # already stored when an operator explicitly downgrades the schema.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_runs) THEN
                RAISE EXCEPTION 'QA records exist; retain row-level security when rolling back the app';
            END IF;
        END;
        $$
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
