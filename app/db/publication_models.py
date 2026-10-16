"""Explicit, immutable corpus membership and atomic active publication selection."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
                        Index, Integer, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CorpusPublication(Base):
    __tablename__ = "corpus_publications"
    __table_args__ = (
        UniqueConstraint("name", name="uq_corpus_publications_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_publications_name"),
        CheckConstraint("status IN ('draft','ready','active','retired')", name="ck_publications_status"),
        CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_publications_hash"),
        CheckConstraint("chunk_count > 0", name="ck_publications_count"),
        CheckConstraint("status <> 'active' OR activated_at IS NOT NULL", name="ck_publications_activation"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("embedding_profiles.id", ondelete="RESTRICT"), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_report: Mapped[dict] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationItem(Base):
    """One deliberately selected chunk set and approved revision per instrument."""
    __tablename__ = "publication_items"
    __table_args__ = (
        ForeignKeyConstraint(["chunk_set_id", "revision_id"], ["chunk_sets.id", "chunk_sets.revision_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["revision_id", "legal_version_id"], ["corpus_revisions.id", "corpus_revisions.legal_version_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["legal_version_id", "instrument_id"], ["legal_versions.id", "legal_versions.instrument_id"], ondelete="RESTRICT"),
        UniqueConstraint("publication_id", "instrument_id", name="uq_publication_items_instrument"),
        UniqueConstraint("publication_id", "revision_id", name="uq_publication_items_revision"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_publication_items_hash"),
        CheckConstraint("chunk_count > 0", name="ck_publication_items_count"),
    )
    publication_id: Mapped[UUID] = mapped_column(ForeignKey("corpus_publications.id", ondelete="RESTRICT"), primary_key=True)
    chunk_set_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    legal_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)


Index("uq_corpus_publications_single_active", CorpusPublication.status,
      unique=True, postgresql_where=text("status = 'active'"))
Index("ix_publication_items_chunk_set", PublicationItem.chunk_set_id)
