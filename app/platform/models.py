"""Private platform records, separate from corpus identities and PDF ingestion."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def now():
    return datetime.now(UTC)


class Record:
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Account(Record, Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (CheckConstraint("role IN ('user','wakeel','admin')"),)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(12), default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    city: Mapped[str] = mapped_column(String(120), default="")
    email_preferences: Mapped[dict] = mapped_column(JSON, default=dict)


class Session(Record, Base):
    __tablename__ = "platform_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WakeelProfile(Base):
    __tablename__ = "platform_wakeels"
    __table_args__ = (CheckConstraint("verification IN ('pending','verified','rejected')"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), primary_key=True)
    bar_number: Mapped[str] = mapped_column(String(120), unique=True)
    specialization: Mapped[str] = mapped_column(String(160))
    experience: Mapped[int] = mapped_column(Integer)
    biography: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    verification_reason: Mapped[str] = mapped_column(Text, default="")
    qualifications: Mapped[str] = mapped_column(Text, default="")
    services: Mapped[str] = mapped_column(Text, default="")
    fees: Mapped[str] = mapped_column(String(200), default="")


class ResetToken(Record, Base):
    __tablename__ = "platform_reset_tokens"
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RateBucket(Base):
    __tablename__ = "platform_rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer)


class Chat(Record, Base):
    __tablename__ = "platform_chats"
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    title_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    title_attempts: Mapped[int] = mapped_column(Integer, default=0)
    title_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocketTicket(Record, Base):
    __tablename__ = "platform_socket_tickets"
    session_id: Mapped[str] = mapped_column(ForeignKey("platform_sessions.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ChatMessage(Record, Base):
    __tablename__ = "platform_chat_messages"
    __table_args__ = (UniqueConstraint("chat_id", "request_id"),)
    chat_id: Mapped[str] = mapped_column(ForeignKey("platform_chats.id"), index=True)
    request_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    question: Mapped[str] = mapped_column(Text)
    response: Mapped[dict] = mapped_column(JSON)


class Conversation(Record, Base):
    __tablename__ = "platform_conversations"
    __table_args__ = (UniqueConstraint("user_id", "wakeel_id"), CheckConstraint("user_id <> wakeel_id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    wakeel_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Record, Base):
    __tablename__ = "platform_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sender_id", "request_id"),
        Index("ix_platform_messages_thread", "conversation_id", "created_at"),
    )
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    request_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    body: Mapped[str] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL is the general relationship thread; a value scopes the message to one case.
    case_id: Mapped[str | None] = mapped_column(ForeignKey("platform_cases.id"), index=True)


class Preparation(Record, Base):
    __tablename__ = "platform_preparations"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    requirements: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PREPARATION_REQUESTED")


class Agreement(Record, Base):
    __tablename__ = "platform_agreements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','SENT','UNDER_REVIEW','REVISION_REQUESTED','NEGOTIATING','DECLINED','WITHDRAWN','SIGNING_PENDING','ACTIVE','AMENDMENT_PENDING','CLOSURE_REQUESTED','COMPLETED','CANCELLED','DISPUTED','EXPIRED','TERMINATION_PENDING','TERMINATED','ARCHIVED','APPROVED','PARTIALLY_SIGNED','FULLY_SIGNED')"
        ),
    )
    # Service agreements belong to a conversation; multi-party agreements use platform_agreement_parties.
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("platform_conversations.id"), index=True)
    preparation_id: Mapped[str | None] = mapped_column(ForeignKey("platform_preparations.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="service", index=True)
    creator_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("platform_relationships.id"), index=True)
    category: Mapped[str] = mapped_column(String(80), default="")
    reference: Mapped[str] = mapped_column(String(40), default="")
    title: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    active_version: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    expires_on: Mapped[str | None] = mapped_column(String(10), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgreementVersion(Record, Base):
    __tablename__ = "platform_agreement_versions"
    __table_args__ = (UniqueConstraint("agreement_id", "number"),)
    agreement_id: Mapped[str] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    terms: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class AgreementEvent(Record, Base):
    __tablename__ = "platform_agreement_events"
    agreement_id: Mapped[str] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    action: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")


class Acceptance(Record, Base):
    __tablename__ = "platform_acceptances"
    __table_args__ = (UniqueConstraint("agreement_id", "version", "user_id", "kind"),)
    agreement_id: Mapped[str] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    kind: Mapped[str] = mapped_column(String(20))
    signed_name: Mapped[str] = mapped_column(String(120))
    digest: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict | None] = mapped_column(JSON)


class Case(Record, Base):
    __tablename__ = "platform_cases"
    agreement_id: Mapped[str | None] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    wakeel_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    important_date: Mapped[str | None] = mapped_column(String(10))
    # Matters opened inside a client-Wakeel engagement. Legacy cases keep NULL.
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("platform_relationships.id"), index=True)
    proposed_by: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"))
    category: Mapped[str] = mapped_column(String(60), default="")
    subcategory: Mapped[str] = mapped_column(String(80), default="")
    legal_issue: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="NORMAL")
    jurisdiction: Mapped[str] = mapped_column(String(160), default="")
    court: Mapped[str] = mapped_column(String(160), default="")
    reference_number: Mapped[str] = mapped_column(String(80), default="")
    opposing_party: Mapped[str] = mapped_column(String(200), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    fee_type: Mapped[str] = mapped_column(String(16), default="FIXED")
    proposed_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    agreed_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="PKR")
    payment_status: Mapped[str] = mapped_column(String(16), default="UNPAID")
    proposed_start: Mapped[str | None] = mapped_column(String(10))
    agreed_start: Mapped[str | None] = mapped_column(String(10))
    expected_completion: Mapped[str | None] = mapped_column(String(10))
    completion_summary: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseUpdate(Record, Base):
    __tablename__ = "platform_case_updates"
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    note: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    important_date: Mapped[str | None] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(24), default="update")
    from_status: Mapped[str | None] = mapped_column(String(32))


class Attachment(Record, Base):
    __tablename__ = "platform_attachments"
    # Exactly one parent: a case, an engagement workspace or a hiring request.
    case_id: Mapped[str | None] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("platform_relationships.id"), index=True)
    hiring_request_id: Mapped[str | None] = mapped_column(ForeignKey("platform_hiring_requests.id"), index=True)
    uploader_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(80))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    title: Mapped[str] = mapped_column(String(180), default="")
    kind: Mapped[str] = mapped_column(String(40), default="other")
    description: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)


class Review(Record, Base):
    __tablename__ = "platform_reviews"
    __table_args__ = (UniqueConstraint("agreement_id", "author_id"), CheckConstraint("rating >= 1 AND rating <= 5"))
    agreement_id: Mapped[str] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    subject_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)


class Notification(Record, Base):
    __tablename__ = "platform_notifications"
    __table_args__ = (Index("ix_platform_notifications_user_unread", "user_id", "read_at"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    href: Mapped[str] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Audit(Record, Base):
    __tablename__ = "platform_audit"
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class Report(Record, Base):
    __tablename__ = "platform_reports"
    reporter_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    resolution: Mapped[str] = mapped_column(Text, default="")


class CaseAgreement(Base):
    __tablename__ = "platform_case_agreements"
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), primary_key=True)
    agreement_id: Mapped[str] = mapped_column(ForeignKey("platform_agreements.id"), primary_key=True, index=True)
    linked_by: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgreementDraft(Base):
    __tablename__ = "platform_agreement_drafts"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    preparation_id: Mapped[str | None] = mapped_column(ForeignKey("platform_preparations.id"))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("platform_cases.id"))
    terms: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OtpChallenge(Record, Base):
    __tablename__ = "platform_otp_challenges"
    # NULL marks a decoy for an unknown address, so both paths behave identically.
    user_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"), unique=True)
    otp_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class MailOutbox(Record, Base):
    __tablename__ = "platform_mail_outbox"
    user_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    # Invitations to people without an account are addressed directly.
    email: Mapped[str | None] = mapped_column(String(254))
    category: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HiringRequest(Record, Base):
    __tablename__ = "platform_hiring_requests"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','UNDER_DISCUSSION','ACCEPTED','REJECTED','CANCELLED')"),
        CheckConstraint("user_id <> wakeel_id"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    wakeel_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"))
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("platform_relationships.id"))
    category: Mapped[str] = mapped_column(String(80))
    requirements: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    communication: Mapped[str] = mapped_column(String(16), default="chat")
    budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    response_reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Relationship(Record, Base):
    """One accepted engagement. Hiring the same Wakeel again creates a new row."""

    __tablename__ = "platform_relationships"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','TERMINATION_REQUESTED','UNDER_REVIEW','ENDED')"),
        Index(
            "uq_platform_relationships_open",
            "user_id",
            "wakeel_id",
            unique=True,
            postgresql_where=text("status <> 'ENDED'"),
            sqlite_where=text("status <> 'ENDED'"),
        ),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    wakeel_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    hiring_request_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"))
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", index=True)
    termination_requested_by: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"))
    termination_reason: Mapped[str] = mapped_column(Text, default="")
    termination_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    termination_note: Mapped[str] = mapped_column(Text, default="")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CaseFee(Record, Base):
    """Every fee proposal is kept; the agreed fee changes only when the other party accepts."""

    __tablename__ = "platform_case_fees"
    __table_args__ = (CheckConstraint("status IN ('PROPOSED','ACCEPTED','REJECTED','SUPERSEDED')"),)
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    proposed_by: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    fee_type: Mapped[str] = mapped_column(String(16), default="FIXED")
    currency: Mapped[str] = mapped_column(String(3), default="PKR")
    advance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    schedule: Mapped[str] = mapped_column(Text, default="")
    expenses: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="PROPOSED")
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(Text, default="")


class CasePayment(Record, Base):
    """A payment counts only after the receiving Wakeel confirms it."""

    __tablename__ = "platform_case_payments"
    __table_args__ = (CheckConstraint("status IN ('PENDING','CONFIRMED','REJECTED')"),)
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    recorded_by: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    method: Mapped[str] = mapped_column(String(40), default="")
    reference: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseDeadline(Record, Base):
    __tablename__ = "platform_case_deadlines"
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    title: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(16), default="deadline")
    due_on: Mapped[str] = mapped_column(String(10), index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)


class Rating(Record, Base):
    """Client ratings: one per closed case and one per ended engagement."""

    __tablename__ = "platform_ratings"
    __table_args__ = (
        UniqueConstraint("author_id", "case_id"),
        UniqueConstraint("author_id", "relationship_id"),
        CheckConstraint("overall >= 1 AND overall <= 5"),
        CheckConstraint("(case_id IS NULL) <> (relationship_id IS NULL)"),
    )
    author_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    subject_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("platform_relationships.id"), index=True)
    overall: Mapped[int] = mapped_column(Integer)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    body: Mapped[str] = mapped_column(Text, default="")


class AgreementParty(Record, Base):
    """A named party to a multi-party agreement. Contents stay hidden until the invitation is accepted."""

    __tablename__ = "platform_agreement_parties"
    __table_args__ = (
        UniqueConstraint("agreement_id", "email"),
        CheckConstraint("invite_status IN ('PENDING','ACCEPTED','DECLINED')"),
        CheckConstraint("approval IN ('PENDING','APPROVED','CHANGES_REQUESTED','REJECTED')"),
    )
    agreement_id: Mapped[str] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(40), default="")
    role: Mapped[str] = mapped_column(String(60), default="Party")
    organization: Mapped[str] = mapped_column(String(160), default="")
    identification: Mapped[str] = mapped_column(String(120), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    signatory: Mapped[bool] = mapped_column(Boolean, default=True)
    invite_status: Mapped[str] = mapped_column(String(12), default="PENDING")
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval: Mapped[str] = mapped_column(String(20), default="PENDING")
    approval_version: Mapped[int | None] = mapped_column(Integer)
    approval_note: Mapped[str] = mapped_column(Text, default="")
    signed_version: Mapped[int | None] = mapped_column(Integer)
