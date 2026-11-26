"""Durable notifications, OTP recovery, drafts and independent case relationships."""

from alembic import op
import sqlalchemy as sa

revision = "20261126_22"
down_revision = "20261126_21"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("platform_accounts", sa.Column("email_preferences", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("platform_agreements", sa.Column("expires_on", sa.String(10), nullable=True))
    op.create_index("ix_platform_agreements_expires_on", "platform_agreements", ["expires_on"])
    # Existing check constraints were unnamed; discover the status constraint.
    for constraint in sa.inspect(op.get_bind()).get_check_constraints("platform_agreements"):
        if "status" in constraint["sqltext"]:
            op.drop_constraint(constraint["name"], "platform_agreements", type_="check")
    states = (
        "DRAFT SENT UNDER_REVIEW REVISION_REQUESTED NEGOTIATING DECLINED WITHDRAWN SIGNING_PENDING ACTIVE "
        "AMENDMENT_PENDING CLOSURE_REQUESTED COMPLETED CANCELLED DISPUTED EXPIRED TERMINATION_PENDING TERMINATED ARCHIVED"
    ).split()
    op.create_check_constraint(
        "ck_agreement_status", "platform_agreements", "status IN (" + ",".join(repr(s) for s in states) + ")"
    )
    op.add_column("platform_cases", sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=True))
    op.add_column("platform_cases", sa.Column("wakeel_id", sa.Uuid(as_uuid=False), nullable=True))
    op.execute("""UPDATE platform_cases c SET user_id=t.user_id, wakeel_id=t.wakeel_id
        FROM platform_agreements a JOIN platform_conversations t ON t.id=a.conversation_id WHERE c.agreement_id=a.id""")
    op.alter_column("platform_cases", "user_id", nullable=False)
    op.alter_column("platform_cases", "agreement_id", nullable=True)
    for name in ("user_id", "wakeel_id"):
        op.create_foreign_key("fk_cases_" + name, "platform_cases", "platform_accounts", [name], ["id"])
        op.create_index("ix_platform_cases_" + name, "platform_cases", [name])
    # Frozen schema: future model edits must not alter this migration.
    op.execute(
        "CREATE TABLE platform_agreement_drafts (\n\tconversation_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\tpreparation_id UUID, \n\tcase_id UUID, \n\tterms JSON NOT NULL, \n\trevision INTEGER NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (conversation_id), \n\tFOREIGN KEY(conversation_id) REFERENCES platform_conversations (id), \n\tFOREIGN KEY(user_id) REFERENCES platform_accounts (id), \n\tFOREIGN KEY(preparation_id) REFERENCES platform_preparations (id), \n\tFOREIGN KEY(case_id) REFERENCES platform_cases (id)\n)"
    )
    op.execute("ALTER TABLE platform_agreement_drafts ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE TABLE platform_case_agreements (\n\tcase_id UUID NOT NULL, \n\tagreement_id UUID NOT NULL, \n\tlinked_by UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (case_id, agreement_id), \n\tFOREIGN KEY(case_id) REFERENCES platform_cases (id), \n\tFOREIGN KEY(agreement_id) REFERENCES platform_agreements (id), \n\tFOREIGN KEY(linked_by) REFERENCES platform_accounts (id)\n)"
    )
    op.create_index("ix_platform_case_agreements_agreement_id", "platform_case_agreements", ["agreement_id"])
    op.execute("ALTER TABLE platform_case_agreements ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE TABLE platform_mail_outbox (\n\tuser_id UUID NOT NULL, \n\tcategory VARCHAR(24) NOT NULL, \n\tpayload JSON NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tavailable_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tclaimed_at TIMESTAMP WITH TIME ZONE, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES platform_accounts (id)\n)"
    )
    op.execute("CREATE INDEX ix_platform_mail_outbox_available_at ON platform_mail_outbox (available_at)")
    op.execute("CREATE INDEX ix_platform_mail_outbox_status ON platform_mail_outbox (status)")
    op.execute("CREATE INDEX ix_platform_mail_outbox_user_id ON platform_mail_outbox (user_id)")
    op.execute("ALTER TABLE platform_mail_outbox ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE TABLE platform_otp_challenges (\n\tuser_id UUID NOT NULL, \n\totp_hash VARCHAR(64) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tused BOOLEAN NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (user_id), \n\tFOREIGN KEY(user_id) REFERENCES platform_accounts (id)\n)"
    )
    op.execute("ALTER TABLE platform_otp_challenges ENABLE ROW LEVEL SECURITY")
    op.execute("""INSERT INTO platform_case_agreements (case_id,agreement_id,linked_by,created_at)
        SELECT id,agreement_id,user_id,created_at FROM platform_cases WHERE agreement_id IS NOT NULL""")
    op.execute("""UPDATE platform_agreements a SET expires_on=v.terms->>'completion_date'
        FROM platform_agreement_versions v WHERE v.agreement_id=a.id AND v.number=a.active_version""")


def downgrade():
    # Independent cases cannot be mapped back without losing data.
    raise RuntimeError("Forward-only migration: restore a verified backup to roll back independent cases.")
