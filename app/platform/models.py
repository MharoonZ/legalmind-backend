"""Private platform records, separate from corpus identities and PDF ingestion."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
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
    __table_args__ = (UniqueConstraint("conversation_id", "sender_id", "request_id"),)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    request_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    body: Mapped[str] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
            "status IN ('DRAFT','SENT','UNDER_REVIEW','REVISION_REQUESTED','NEGOTIATING','DECLINED','WITHDRAWN','SIGNING_PENDING','ACTIVE','AMENDMENT_PENDING','CLOSURE_REQUESTED','COMPLETED','CANCELLED','DISPUTED','EXPIRED','TERMINATION_PENDING','TERMINATED','ARCHIVED')"
        ),
    )
    conversation_id: Mapped[str] = mapped_column(ForeignKey("platform_conversations.id"), index=True)
    preparation_id: Mapped[str | None] = mapped_column(ForeignKey("platform_preparations.id"), unique=True)
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


class Case(Record, Base):
    __tablename__ = "platform_cases"
    agreement_id: Mapped[str | None] = mapped_column(ForeignKey("platform_agreements.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    wakeel_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    important_date: Mapped[str | None] = mapped_column(String(10))


class CaseUpdate(Record, Base):
    __tablename__ = "platform_case_updates"
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    note: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    important_date: Mapped[str | None] = mapped_column(String(10))


class Attachment(Record, Base):
    __tablename__ = "platform_attachments"
    case_id: Mapped[str] = mapped_column(ForeignKey("platform_cases.id"), index=True)
    uploader_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"))
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(80))
    content: Mapped[bytes] = mapped_column(LargeBinary)


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
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    href: Mapped[str] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Audit(Record, Base):
    __tablename__ = "platform_audit"
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("platform_accounts.id"))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
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
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), unique=True)
    otp_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class MailOutbox(Record, Base):
    __tablename__ = "platform_mail_outbox"
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    category: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
