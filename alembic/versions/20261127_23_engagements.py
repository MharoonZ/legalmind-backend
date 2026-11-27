"""Hiring, engagements, case negotiation and finance, ratings and multi-party agreements.

Additive: existing accounts, messages, agreements, cases and files are kept as they are.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261127_23"
down_revision = "20261126_22"
branch_labels = None
depends_on = None

UUID = sa.Uuid(as_uuid=False)
TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(12, 2)


def record():
    return [sa.Column("id", UUID, primary_key=True), sa.Column("created_at", TS, nullable=False)]


def account(name, nullable=False, index=False):
    return sa.Column(name, UUID, sa.ForeignKey("platform_accounts.id"), nullable=nullable, index=index)


def text(name, default=""):
    return sa.Column(name, sa.Text(), nullable=False, server_default=default)


def string(name, length, default=""):
    return sa.Column(name, sa.String(length), nullable=False, server_default=default)


def private(table):
    # The backend role is the only reader; no public client policies exist.
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def upgrade():
    # ------------------------------------------------------------ engagements
    op.create_table(
        "platform_relationships",
        *record(),
        account("user_id", index=True),
        account("wakeel_id", index=True),
        sa.Column("hiring_request_id", UUID),
        sa.Column("conversation_id", UUID, sa.ForeignKey("platform_conversations.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE", index=True),
        account("termination_requested_by", nullable=True),
        text("termination_reason"),
        sa.Column("termination_requested_at", TS),
        text("termination_note"),
        sa.Column("ended_at", TS),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','TERMINATION_REQUESTED','UNDER_REVIEW','ENDED')", name="ck_relationship_status"
        ),
    )
    # One open engagement per client-Wakeel pair; ended ones remain as history.
    op.create_index(
        "uq_platform_relationships_open",
        "platform_relationships",
        ["user_id", "wakeel_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'ENDED'"),
    )
    op.create_table(
        "platform_hiring_requests",
        *record(),
        account("user_id", index=True),
        account("wakeel_id", index=True),
        sa.Column("conversation_id", UUID, sa.ForeignKey("platform_conversations.id"), nullable=False),
        sa.Column("relationship_id", UUID, sa.ForeignKey("platform_relationships.id")),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        string("communication", 16, "chat"),
        sa.Column("budget", MONEY),
        text("notes"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING", index=True),
        text("response_reason"),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','UNDER_DISCUSSION','ACCEPTED','REJECTED','CANCELLED')", name="ck_hiring_status"
        ),
        sa.CheckConstraint("user_id <> wakeel_id", name="ck_hiring_parties"),
    )

    # ------------------------------------------------------------------ cases
    op.alter_column("platform_cases", "status", type_=sa.String(32))
    op.alter_column("platform_case_updates", "status", type_=sa.String(32))
    op.create_index("ix_platform_cases_status", "platform_cases", ["status"])
    for column in (
        sa.Column("relationship_id", UUID, sa.ForeignKey("platform_relationships.id")),
        account("proposed_by", nullable=True),
        string("category", 60),
        string("subcategory", 80),
        text("legal_issue"),
        string("priority", 10, "NORMAL"),
        string("jurisdiction", 160),
        string("court", 160),
        string("reference_number", 80),
        string("opposing_party", 200),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        string("fee_type", 16, "FIXED"),
        sa.Column("proposed_fee", MONEY),
        sa.Column("agreed_fee", MONEY),
        string("currency", 3, "PKR"),
        string("payment_status", 16, "UNPAID"),
        sa.Column("proposed_start", sa.String(10)),
        sa.Column("agreed_start", sa.String(10)),
        sa.Column("expected_completion", sa.String(10)),
        text("completion_summary"),
        sa.Column("completed_at", TS),
        sa.Column("closed_at", TS),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    ):
        op.add_column("platform_cases", column)
    op.execute("UPDATE platform_cases SET updated_at = created_at")
    op.create_index("ix_platform_cases_relationship_id", "platform_cases", ["relationship_id"])
    op.add_column("platform_case_updates", string("kind", 24, "update"))
    op.add_column("platform_case_updates", sa.Column("from_status", sa.String(32)))

    op.create_table(
        "platform_case_fees",
        *record(),
        sa.Column("case_id", UUID, sa.ForeignKey("platform_cases.id"), nullable=False, index=True),
        account("proposed_by"),
        sa.Column("amount", MONEY, nullable=False),
        string("fee_type", 16, "FIXED"),
        string("currency", 3, "PKR"),
        sa.Column("advance", MONEY),
        text("schedule"),
        text("expenses"),
        text("note"),
        string("status", 12, "PROPOSED"),
        account("decided_by", nullable=True),
        sa.Column("decided_at", TS),
        text("decision_note"),
        sa.CheckConstraint("status IN ('PROPOSED','ACCEPTED','REJECTED','SUPERSEDED')", name="ck_case_fee_status"),
    )
    op.create_table(
        "platform_case_payments",
        *record(),
        sa.Column("case_id", UUID, sa.ForeignKey("platform_cases.id"), nullable=False, index=True),
        account("recorded_by"),
        sa.Column("amount", MONEY, nullable=False),
        string("method", 40),
        string("reference", 120),
        text("note"),
        string("status", 12, "PENDING"),
        account("decided_by", nullable=True),
        sa.Column("decided_at", TS),
        sa.CheckConstraint("status IN ('PENDING','CONFIRMED','REJECTED')", name="ck_case_payment_status"),
    )
    op.create_table(
        "platform_case_deadlines",
        *record(),
        sa.Column("case_id", UUID, sa.ForeignKey("platform_cases.id"), nullable=False, index=True),
        account("created_by"),
        sa.Column("title", sa.String(180), nullable=False),
        string("kind", 16, "deadline"),
        sa.Column("due_on", sa.String(10), nullable=False, index=True),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "platform_ratings",
        *record(),
        account("author_id"),
        account("subject_id", index=True),
        sa.Column("case_id", UUID, sa.ForeignKey("platform_cases.id"), index=True),
        sa.Column("relationship_id", UUID, sa.ForeignKey("platform_relationships.id"), index=True),
        sa.Column("overall", sa.Integer(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False, server_default="{}"),
        text("body"),
        sa.UniqueConstraint("author_id", "case_id", name="uq_rating_case"),
        sa.UniqueConstraint("author_id", "relationship_id", name="uq_rating_relationship"),
        sa.CheckConstraint("overall >= 1 AND overall <= 5", name="ck_rating_overall"),
        sa.CheckConstraint("(case_id IS NULL) <> (relationship_id IS NULL)", name="ck_rating_subject"),
    )

    # ----------------------------------------------------- messages, documents
    op.add_column("platform_messages", sa.Column("case_id", UUID, sa.ForeignKey("platform_cases.id")))
    op.create_index("ix_platform_messages_case_id", "platform_messages", ["case_id"])
    op.create_index("ix_platform_messages_thread", "platform_messages", ["conversation_id", "created_at"])
    op.alter_column("platform_attachments", "case_id", nullable=True)
    for column in (
        sa.Column("relationship_id", UUID, sa.ForeignKey("platform_relationships.id")),
        sa.Column("hiring_request_id", UUID, sa.ForeignKey("platform_hiring_requests.id")),
        string("title", 180),
        string("kind", 40, "other"),
        text("description"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("platform_attachments", column)
    op.execute("UPDATE platform_attachments SET title = filename, size = octet_length(content)")
    for name in ("relationship_id", "hiring_request_id", "uploader_id"):
        op.create_index("ix_platform_attachments_" + name, "platform_attachments", [name])

    # ------------------------------------------------------------- agreements
    op.alter_column("platform_agreements", "conversation_id", nullable=True)
    for column in (
        string("kind", 16, "service"),
        account("creator_id", nullable=True),
        sa.Column("relationship_id", UUID, sa.ForeignKey("platform_relationships.id")),
        string("category", 80),
        string("reference", 40),
    ):
        op.add_column("platform_agreements", column)
    for name in ("kind", "creator_id", "relationship_id"):
        op.create_index("ix_platform_agreements_" + name, "platform_agreements", [name])
    op.execute(
        "UPDATE platform_agreements a SET creator_id = t.wakeel_id, category = 'Legal service agreement' "
        "FROM platform_conversations t WHERE t.id = a.conversation_id"
    )
    for constraint in sa.inspect(op.get_bind()).get_check_constraints("platform_agreements"):
        if "status" in constraint["sqltext"]:
            op.drop_constraint(constraint["name"], "platform_agreements", type_="check")
    states = (
        "DRAFT SENT UNDER_REVIEW REVISION_REQUESTED NEGOTIATING DECLINED WITHDRAWN SIGNING_PENDING ACTIVE "
        "AMENDMENT_PENDING CLOSURE_REQUESTED COMPLETED CANCELLED DISPUTED EXPIRED TERMINATION_PENDING TERMINATED "
        "ARCHIVED APPROVED PARTIALLY_SIGNED FULLY_SIGNED"
    ).split()
    op.create_check_constraint(
        "ck_agreement_status", "platform_agreements", "status IN (" + ",".join(repr(s) for s in states) + ")"
    )
    op.add_column("platform_acceptances", sa.Column("evidence", sa.JSON()))
    op.create_table(
        "platform_agreement_parties",
        *record(),
        sa.Column("agreement_id", UUID, sa.ForeignKey("platform_agreements.id"), nullable=False, index=True),
        account("account_id", nullable=True, index=True),
        sa.Column("email", sa.String(254), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        string("phone", 40),
        string("role", 60, "Party"),
        string("organization", 160),
        string("identification", 120),
        text("address"),
        sa.Column("signatory", sa.Boolean(), nullable=False, server_default=sa.true()),
        string("invite_status", 12, "PENDING"),
        sa.Column("invite_token_hash", sa.String(64), index=True),
        sa.Column("invited_at", TS),
        sa.Column("responded_at", TS),
        string("approval", 20, "PENDING"),
        sa.Column("approval_version", sa.Integer()),
        text("approval_note"),
        sa.Column("signed_version", sa.Integer()),
        sa.UniqueConstraint("agreement_id", "email", name="uq_agreement_party_email"),
        sa.CheckConstraint("invite_status IN ('PENDING','ACCEPTED','DECLINED')", name="ck_party_invite"),
        sa.CheckConstraint(
            "approval IN ('PENDING','APPROVED','CHANGES_REQUESTED','REJECTED')", name="ck_party_approval"
        ),
    )

    # ------------------------------------------- profiles, recovery, mail, audit
    for column in (text("qualifications"), text("services"), string("fees", 200)):
        op.add_column("platform_wakeels", column)
    op.alter_column("platform_otp_challenges", "user_id", nullable=True)
    op.alter_column("platform_mail_outbox", "user_id", nullable=True)
    op.add_column("platform_mail_outbox", sa.Column("email", sa.String(254)))
    op.create_index("ix_platform_audit_resource_id", "platform_audit", ["resource_id"])
    op.create_index("ix_platform_notifications_user_unread", "platform_notifications", ["user_id", "read_at"])

    for table in (
        "platform_relationships",
        "platform_hiring_requests",
        "platform_case_fees",
        "platform_case_payments",
        "platform_case_deadlines",
        "platform_ratings",
        "platform_agreement_parties",
    ):
        private(table)


def downgrade():
    # Engagement history, multi-party agreements and nullable parents cannot be mapped back safely.
    raise RuntimeError("Forward-only migration: restore a verified backup to roll back engagements.")
