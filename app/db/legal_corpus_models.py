"""Haroon's additive legal-PDF ingestion and reviewed-structure schema.

These tables coexist with the legacy 30-section corpus. No user-facing search
switches to them until a later, explicit indexing/publication migration.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _id_column():
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)


class AppUser(Base):
    """Identity used for admin uploads and review, not a password store."""

    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint("auth_provider", "auth_subject", name="uq_app_users_identity"),
        CheckConstraint("role IN ('admin', 'user')", name="ck_app_users_role"),
    )

    id: Mapped[UUID] = _id_column()
    auth_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    auth_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Jurisdiction(Base):
    __tablename__ = "jurisdictions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_jurisdictions_code"),
        CheckConstraint("length(btrim(code)) > 0", name="ck_jurisdictions_code_nonempty"),
    )

    id: Mapped[UUID] = _id_column()
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("jurisdictions.id", ondelete="RESTRICT"))


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_categories_slug"),
        CheckConstraint("length(btrim(slug)) > 0", name="ck_categories_slug_nonempty"),
    )

    id: Mapped[UUID] = _id_column()
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))


class LegalInstrument(Base):
    """An Act or Rules as a stable identity, independent of edition and PDF."""

    __tablename__ = "legal_instruments"
    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "citation_key", name="uq_legal_instruments_jurisdiction_key"),
        CheckConstraint("instrument_type IN ('act', 'rule', 'regulation', 'ordinance', 'other')", name="ck_legal_instruments_type"),
        CheckConstraint("length(btrim(citation_key)) > 0", name="ck_legal_instruments_key_nonempty"),
    )

    id: Mapped[UUID] = _id_column()
    jurisdiction_id: Mapped[UUID] = mapped_column(ForeignKey("jurisdictions.id", ondelete="RESTRICT"), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_title: Mapped[str] = mapped_column(Text, nullable=False)
    citation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InstrumentCategory(Base):
    __tablename__ = "instrument_categories"
    __table_args__ = (PrimaryKeyConstraint("instrument_id", "category_id"),)

    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("legal_instruments.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[UUID] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)


class LegalVersion(Base):
    """A legal edition; an extraction correction is NOT a new edition."""

    __tablename__ = "legal_versions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "edition_key", name="uq_legal_versions_instrument_edition"),
        CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from", name="ck_legal_versions_effective_range"),
        CheckConstraint("applicability_status IN ('verified', 'unverified', 'unknown')", name="ck_legal_versions_applicability"),
    )

    id: Mapped[UUID] = _id_column()
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("legal_instruments.id", ondelete="RESTRICT"), nullable=False)
    edition_key: Mapped[str] = mapped_column(String(160), nullable=False)
    publication_date: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    applicability_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unknown")
    date_source: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegalFile(Base):
    """Immutable source PDF bytes, stored outside PostgreSQL."""

    __tablename__ = "legal_files"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_legal_files_object_key"),
        UniqueConstraint("sha256", name="uq_legal_files_sha256"),
        CheckConstraint("byte_size > 0", name="ck_legal_files_byte_size"),
        CheckConstraint("page_count > 0", name="ck_legal_files_page_count"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_legal_files_sha256"),
    )

    id: Mapped[UUID] = _id_column()
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(200))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegalFileAssignment(Base):
    """Admin's durable upload-time association of a PDF with a legal edition."""

    __tablename__ = "legal_file_assignments"
    __table_args__ = (
        PrimaryKeyConstraint("file_id", "legal_version_id"),
        CheckConstraint("assignment_role IN ('primary', 'supplement', 'amendment')", name="ck_legal_file_assignments_role"),
    )

    file_id: Mapped[UUID] = mapped_column(ForeignKey("legal_files.id", ondelete="RESTRICT"), nullable=False)
    legal_version_id: Mapped[UUID] = mapped_column(ForeignKey("legal_versions.id", ondelete="RESTRICT"), nullable=False)
    assignment_role: Mapped[str] = mapped_column(String(20), nullable=False)
    assigned_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FileExtraction(Base):
    """One output of a specific extraction/OCR configuration for one PDF."""

    __tablename__ = "file_extractions"
    __table_args__ = (
        UniqueConstraint("id", "file_id", name="uq_file_extractions_id_file"),
        CheckConstraint("processing_status IN ('pending', 'running', 'completed', 'failed')", name="ck_file_extractions_status"),
        CheckConstraint("processing_status <> 'completed' OR (output_sha256 IS NOT NULL AND completed_at IS NOT NULL)", name="ck_file_extractions_completed_output"),
    )

    id: Mapped[UUID] = _id_column()
    file_id: Mapped[UUID] = mapped_column(ForeignKey("legal_files.id", ondelete="RESTRICT"), nullable=False)
    engine_name: Mapped[str] = mapped_column(String(100), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    ocr_engine: Mapped[str | None] = mapped_column(String(100))
    ocr_languages: Mapped[str | None] = mapped_column(String(100))
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FilePage(Base):
    """Every physical PDF page must eventually have a row and an outcome."""

    __tablename__ = "file_pages"
    __table_args__ = (
        UniqueConstraint("extraction_id", "page_index", name="uq_file_pages_extraction_index"),
        UniqueConstraint("id", "extraction_id", name="uq_file_pages_id_extraction"),
        CheckConstraint("page_index > 0", name="ck_file_pages_index"),
        CheckConstraint("extraction_method IN ('native', 'ocr', 'mixed', 'none')", name="ck_file_pages_method"),
        CheckConstraint("outcome IN ('extracted', 'verified_blank', 'excluded_with_reason', 'failed', 'needs_review')", name="ck_file_pages_outcome"),
        CheckConstraint("outcome <> 'excluded_with_reason' OR (exclusion_reason IS NOT NULL AND length(btrim(exclusion_reason)) > 0)", name="ck_file_pages_exclusion_reason"),
        CheckConstraint("ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)", name="ck_file_pages_ocr_confidence"),
        CheckConstraint("(width_pt IS NULL AND height_pt IS NULL) OR (width_pt IS NOT NULL AND height_pt IS NOT NULL AND width_pt > 0 AND height_pt > 0)", name="ck_file_pages_dimensions"),
        CheckConstraint("rotation_deg IS NULL OR rotation_deg IN (0, 90, 180, 270)", name="ck_file_pages_rotation"),
    )

    id: Mapped[UUID] = _id_column()
    extraction_id: Mapped[UUID] = mapped_column(ForeignKey("file_extractions.id", ondelete="RESTRICT"), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_label: Mapped[str | None] = mapped_column(String(100))
    width_pt: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    height_pt: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    rotation_deg: Mapped[int | None] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    extraction_method: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FileBlock(Base):
    """Inspectable ordered paragraph/heading/table within a page."""

    __tablename__ = "file_blocks"
    __table_args__ = (
        ForeignKeyConstraint(["page_id", "extraction_id"], ["file_pages.id", "file_pages.extraction_id"], ondelete="RESTRICT"),
        UniqueConstraint("page_id", "block_ordinal", name="uq_file_blocks_page_ordinal"),
        UniqueConstraint("id", "page_id", "extraction_id", name="uq_file_blocks_id_page_extraction"),
        CheckConstraint("block_ordinal > 0", name="ck_file_blocks_ordinal"),
        CheckConstraint("block_kind IN ('heading', 'paragraph', 'table', 'footnote', 'note', 'header', 'footer', 'figure', 'other')", name="ck_file_blocks_kind"),
        CheckConstraint("(page_char_start IS NULL AND page_char_end IS NULL) OR (page_char_start IS NOT NULL AND page_char_end IS NOT NULL AND page_char_start >= 0 AND page_char_end > page_char_start)", name="ck_file_blocks_page_range"),
    )

    id: Mapped[UUID] = _id_column()
    extraction_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    page_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    block_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    block_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    page_char_start: Mapped[int | None] = mapped_column(Integer)
    page_char_end: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict | None] = mapped_column(JSONB)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CorpusRevision(Base):
    """Reviewed structured transcription of one legal edition."""

    __tablename__ = "corpus_revisions"
    __table_args__ = (
        UniqueConstraint("legal_version_id", "revision_number", name="uq_corpus_revisions_version_number"),
        UniqueConstraint("id", "legal_version_id", name="uq_corpus_revisions_id_version"),
        CheckConstraint("revision_number > 0", name="ck_corpus_revisions_number"),
        CheckConstraint("review_state IN ('draft', 'needs_review', 'approved', 'rejected')", name="ck_corpus_revisions_state"),
        CheckConstraint("(review_state = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL AND content_sha256 IS NOT NULL) OR (review_state <> 'approved' AND approved_by IS NULL AND approved_at IS NULL)", name="ck_corpus_revisions_approval"),
    )

    id: Mapped[UUID] = _id_column()
    legal_version_id: Mapped[UUID] = mapped_column(ForeignKey("legal_versions.id", ondelete="RESTRICT"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    review_state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RevisionSource(Base):
    """Which PDF extraction supplies a revision; spans select exact content."""

    __tablename__ = "revision_sources"
    __table_args__ = (
        PrimaryKeyConstraint("revision_id", "extraction_id"),
        ForeignKeyConstraint(["revision_id", "legal_version_id"], ["corpus_revisions.id", "corpus_revisions.legal_version_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["extraction_id", "file_id"], ["file_extractions.id", "file_extractions.file_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["file_id", "legal_version_id"], ["legal_file_assignments.file_id", "legal_file_assignments.legal_version_id"], ondelete="RESTRICT"),
        CheckConstraint("source_role IN ('primary', 'supplement', 'amendment')", name="ck_revision_sources_role"),
    )

    revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    legal_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    extraction_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    file_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_role: Mapped[str] = mapped_column(String(20), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Provision(Base):
    """One ordered legal structure node; labels are never constrained to 1–30."""

    __tablename__ = "provisions"
    __table_args__ = (
        ForeignKeyConstraint(["parent_id", "revision_id"], ["provisions.id", "provisions.revision_id"], ondelete="RESTRICT"),
        UniqueConstraint("id", "revision_id", name="uq_provisions_id_revision"),
        UniqueConstraint("revision_id", "ordinal", name="uq_provisions_revision_ordinal"),
        UniqueConstraint("revision_id", "reference_path", name="uq_provisions_revision_path"),
        CheckConstraint("ordinal > 0", name="ck_provisions_ordinal"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_provisions_no_self_parent"),
        CheckConstraint("kind IN ('preamble', 'part', 'chapter', 'section', 'subsection', 'paragraph', 'rule', 'schedule', 'article', 'other')", name="ck_provisions_kind"),
    )

    id: Mapped[UUID] = _id_column()
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("corpus_revisions.id", ondelete="RESTRICT"), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    reference_path: Mapped[str | None] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text)
    own_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProvisionSpan(Base):
    """Scope-safe mapping from provision text to a selected PDF page/block."""

    __tablename__ = "provision_spans"
    __table_args__ = (
        ForeignKeyConstraint(["provision_id", "revision_id"], ["provisions.id", "provisions.revision_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["revision_id", "extraction_id"], ["revision_sources.revision_id", "revision_sources.extraction_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["page_id", "extraction_id"], ["file_pages.id", "file_pages.extraction_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["block_id", "page_id", "extraction_id"], ["file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id"], ondelete="RESTRICT"),
        UniqueConstraint("provision_id", "provision_char_start", "provision_char_end", "page_id", "block_id", name="uq_provision_spans_range_source"),
        CheckConstraint("provision_char_start >= 0 AND provision_char_end > provision_char_start", name="ck_provision_spans_provision_range"),
        CheckConstraint("(block_char_start IS NULL AND block_char_end IS NULL) OR (block_id IS NOT NULL AND block_char_start IS NOT NULL AND block_char_end IS NOT NULL AND block_char_start >= 0 AND block_char_end > block_char_start)", name="ck_provision_spans_block_range"),
        CheckConstraint("mapping_quality IN ('exact', 'approximate', 'page_only')", name="ck_provision_spans_quality"),
        CheckConstraint("mapping_quality <> 'exact' OR (block_id IS NOT NULL AND block_char_start IS NOT NULL AND block_char_end IS NOT NULL)", name="ck_provision_spans_exact_source"),
    )

    id: Mapped[UUID] = _id_column()
    revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    provision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    extraction_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    page_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    block_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    provision_char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    provision_char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    block_char_start: Mapped[int | None] = mapped_column(Integer)
    block_char_end: Mapped[int | None] = mapped_column(Integer)
    mapping_quality: Mapped[str] = mapped_column(String(20), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class ProvisionReference(Base):
    __tablename__ = "provision_references"
    __table_args__ = (
        CheckConstraint("resolution_status IN ('unresolved', 'resolved', 'ambiguous', 'rejected')", name="ck_provision_references_status"),
        CheckConstraint("resolution_status <> 'resolved' OR (target_instrument_id IS NOT NULL AND target_reference_path IS NOT NULL)", name="ck_provision_references_resolved_target"),
    )

    id: Mapped[UUID] = _id_column()
    source_provision_id: Mapped[UUID] = mapped_column(ForeignKey("provisions.id", ondelete="RESTRICT"), nullable=False)
    raw_reference: Mapped[str] = mapped_column(Text, nullable=False)
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("legal_instruments.id", ondelete="RESTRICT"))
    target_reference_path: Mapped[str | None] = mapped_column(Text)
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unresolved")
    notes: Mapped[str | None] = mapped_column(Text)


class InstrumentRelation(Base):
    __tablename__ = "instrument_relations"
    __table_args__ = (
        CheckConstraint("from_instrument_id <> to_instrument_id", name="ck_instrument_relations_distinct"),
        CheckConstraint("relation_type IN ('made_under', 'amends', 'repeals', 'supersedes', 'related_to')", name="ck_instrument_relations_type"),
        CheckConstraint("review_status IN ('proposed', 'reviewed', 'rejected')", name="ck_instrument_relations_status"),
        CheckConstraint("review_status <> 'reviewed' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="ck_instrument_relations_review_record"),
    )

    id: Mapped[UUID] = _id_column()
    from_instrument_id: Mapped[UUID] = mapped_column(ForeignKey("legal_instruments.id", ondelete="RESTRICT"), nullable=False)
    to_instrument_id: Mapped[UUID] = mapped_column(ForeignKey("legal_instruments.id", ondelete="RESTRICT"), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_provision_id: Mapped[UUID | None] = mapped_column(ForeignKey("provisions.id", ondelete="RESTRICT"))
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="proposed")
    reviewed_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_note: Mapped[str | None] = mapped_column(Text)


class ReviewEvent(Base):
    """One reviewer action targeting an extraction or structured revision."""

    __tablename__ = "review_events"
    __table_args__ = (
        ForeignKeyConstraint(["page_id", "file_extraction_id"], ["file_pages.id", "file_pages.extraction_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["block_id", "page_id", "file_extraction_id"], ["file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id"], ondelete="RESTRICT"),
        CheckConstraint("num_nonnulls(file_extraction_id, corpus_revision_id) = 1", name="ck_review_events_one_target"),
        CheckConstraint("page_id IS NULL OR file_extraction_id IS NOT NULL", name="ck_review_events_page_target"),
        CheckConstraint("block_id IS NULL OR page_id IS NOT NULL", name="ck_review_events_block_target"),
        CheckConstraint("decision IN ('approve', 'reject', 'request_changes', 'accept', 'mark_excluded', 'mark_blank')", name="ck_review_events_decision"),
    )

    id: Mapped[UUID] = _id_column()
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False)
    file_extraction_id: Mapped[UUID | None] = mapped_column(ForeignKey("file_extractions.id", ondelete="RESTRICT"))
    corpus_revision_id: Mapped[UUID | None] = mapped_column(ForeignKey("corpus_revisions.id", ondelete="RESTRICT"))
    page_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    block_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProcessingJob(Base):
    """Durable work request; target identifies file, extraction or revision."""

    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("task_type", "idempotency_key", name="uq_processing_jobs_type_idempotency"),
        CheckConstraint("num_nonnulls(target_file_id, target_extraction_id, target_revision_id) = 1", name="ck_processing_jobs_one_target"),
        CheckConstraint("task_type IN ('extract', 'structure', 'verify')", name="ck_processing_jobs_type"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'cancelled')", name="ck_processing_jobs_status"),
        CheckConstraint("progress_total IS NULL OR progress_total >= 0", name="ck_processing_jobs_total"),
        CheckConstraint("progress_done >= 0 AND (progress_total IS NULL OR progress_done <= progress_total)", name="ck_processing_jobs_progress"),
    )

    id: Mapped[UUID] = _id_column()
    task_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_file_id: Mapped[UUID | None] = mapped_column(ForeignKey("legal_files.id", ondelete="RESTRICT"))
    target_extraction_id: Mapped[UUID | None] = mapped_column(ForeignKey("file_extractions.id", ondelete="RESTRICT"))
    target_revision_id: Mapped[UUID | None] = mapped_column(ForeignKey("corpus_revisions.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_number"),
        CheckConstraint("attempt_number > 0", name="ck_job_attempts_number"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_job_attempts_status"),
    )

    id: Mapped[UUID] = _id_column()
    job_id: Mapped[UUID] = mapped_column(ForeignKey("processing_jobs.id", ondelete="RESTRICT"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


Index("ix_legal_versions_instrument_id", LegalVersion.instrument_id)
Index("ix_file_extractions_file_id", FileExtraction.file_id)
Index("ix_legal_file_assignments_version_id", LegalFileAssignment.legal_version_id)
Index("ix_file_blocks_page_id", FileBlock.page_id)
Index("ix_revision_sources_extraction_id", RevisionSource.extraction_id)
Index("ix_provisions_revision_parent", Provision.revision_id, Provision.parent_id)
Index("ix_provision_spans_provision_id", ProvisionSpan.provision_id)
Index("ix_provision_spans_page_id", ProvisionSpan.page_id)
Index("ix_provision_references_source", ProvisionReference.source_provision_id)
Index("ix_instrument_relations_from", InstrumentRelation.from_instrument_id)
Index("ix_instrument_relations_to", InstrumentRelation.to_instrument_id)
Index("ix_review_events_extraction", ReviewEvent.file_extraction_id)
Index("ix_review_events_revision", ReviewEvent.corpus_revision_id)
Index("ix_processing_jobs_status_lease", ProcessingJob.status, ProcessingJob.lease_until)
Index("ix_job_attempts_job_id", JobAttempt.job_id)
