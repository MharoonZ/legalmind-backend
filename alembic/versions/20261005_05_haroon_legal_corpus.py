"""Add Haroon's reviewed, full-PDF legal corpus schema.

Revision ID: 20261005_05
Revises: 20260927_04

Additive only: existing law_sources/law_sections/law_passages and private
contract tables are not modified or silently migrated.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261005_05"
down_revision = "20260927_04"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def _id():
    return sa.Column("id", UUID, primary_key=True)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _fk(name, target, *, nullable=False, ondelete="RESTRICT"):
    return sa.Column(name, UUID, sa.ForeignKey(target, ondelete=ondelete), nullable=nullable)


def _uuid(name, *, nullable=False):
    return sa.Column(name, UUID, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "app_users",
        _id(),
        sa.Column("auth_provider", sa.String(50), nullable=False),
        sa.Column("auth_subject", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        _created(),
        sa.UniqueConstraint("auth_provider", "auth_subject", name="uq_app_users_identity"),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_app_users_role"),
    )
    op.create_table(
        "jurisdictions",
        _id(),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _fk("parent_id", "jurisdictions.id", nullable=True),
        sa.UniqueConstraint("code", name="uq_jurisdictions_code"),
        sa.CheckConstraint("length(btrim(code)) > 0", name="ck_jurisdictions_code_nonempty"),
    )
    op.create_table(
        "categories",
        _id(),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _fk("parent_id", "categories.id", nullable=True),
        sa.UniqueConstraint("slug", name="uq_categories_slug"),
        sa.CheckConstraint("length(btrim(slug)) > 0", name="ck_categories_slug_nonempty"),
    )
    op.create_table(
        "legal_instruments",
        _id(),
        _fk("jurisdiction_id", "jurisdictions.id"),
        sa.Column("instrument_type", sa.String(20), nullable=False),
        sa.Column("canonical_title", sa.Text(), nullable=False),
        sa.Column("citation_key", sa.String(160), nullable=False),
        sa.Column("aliases", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _created(),
        sa.UniqueConstraint("jurisdiction_id", "citation_key", name="uq_legal_instruments_jurisdiction_key"),
        sa.CheckConstraint("instrument_type IN ('act', 'rule', 'regulation', 'ordinance', 'other')", name="ck_legal_instruments_type"),
        sa.CheckConstraint("length(btrim(citation_key)) > 0", name="ck_legal_instruments_key_nonempty"),
    )
    op.create_table(
        "instrument_categories",
        _fk("instrument_id", "legal_instruments.id", ondelete="CASCADE"),
        _fk("category_id", "categories.id"),
        sa.PrimaryKeyConstraint("instrument_id", "category_id"),
    )
    op.create_table(
        "legal_versions",
        _id(),
        _fk("instrument_id", "legal_instruments.id"),
        sa.Column("edition_key", sa.String(160), nullable=False),
        sa.Column("publication_date", sa.Date()),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("applicability_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("date_source", sa.Text()),
        sa.Column("notes", sa.Text()),
        _created(),
        sa.UniqueConstraint("instrument_id", "edition_key", name="uq_legal_versions_instrument_edition"),
        sa.CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from", name="ck_legal_versions_effective_range"),
        sa.CheckConstraint("applicability_status IN ('verified', 'unverified', 'unknown')", name="ck_legal_versions_applicability"),
    )
    op.create_table(
        "legal_files",
        _id(),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("publisher", sa.String(200)),
        sa.Column("acquired_at", sa.DateTime(timezone=True)),
        _fk("uploaded_by", "app_users.id", nullable=True),
        _created(),
        sa.UniqueConstraint("object_key", name="uq_legal_files_object_key"),
        sa.UniqueConstraint("sha256", name="uq_legal_files_sha256"),
        sa.CheckConstraint("byte_size > 0", name="ck_legal_files_byte_size"),
        sa.CheckConstraint("page_count > 0", name="ck_legal_files_page_count"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_legal_files_sha256"),
    )
    op.create_table(
        "legal_file_assignments",
        _fk("file_id", "legal_files.id"),
        _fk("legal_version_id", "legal_versions.id"),
        sa.Column("assignment_role", sa.String(20), nullable=False),
        _fk("assigned_by", "app_users.id", nullable=True),
        sa.Column("notes", sa.Text()),
        _created(),
        sa.PrimaryKeyConstraint("file_id", "legal_version_id"),
        sa.CheckConstraint("assignment_role IN ('primary', 'supplement', 'amendment')", name="ck_legal_file_assignments_role"),
    )
    op.create_table(
        "file_extractions",
        _id(),
        _fk("file_id", "legal_files.id"),
        sa.Column("engine_name", sa.String(100), nullable=False),
        sa.Column("engine_version", sa.String(100), nullable=False),
        sa.Column("ocr_engine", sa.String(100)),
        sa.Column("ocr_languages", sa.String(100)),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("output_sha256", sa.String(64)),
        sa.Column("processing_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        _created(),
        sa.UniqueConstraint("id", "file_id", name="uq_file_extractions_id_file"),
        sa.CheckConstraint("processing_status IN ('pending', 'running', 'completed', 'failed')", name="ck_file_extractions_status"),
        sa.CheckConstraint("processing_status <> 'completed' OR (output_sha256 IS NOT NULL AND completed_at IS NOT NULL)", name="ck_file_extractions_completed_output"),
    )
    op.create_table(
        "file_pages",
        _id(),
        _fk("extraction_id", "file_extractions.id"),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("printed_label", sa.String(100)),
        sa.Column("width_pt", sa.Numeric(10, 3)),
        sa.Column("height_pt", sa.Numeric(10, 3)),
        sa.Column("rotation_deg", sa.Integer()),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_sha256", sa.String(64)),
        sa.Column("extraction_method", sa.String(20), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("exclusion_reason", sa.Text()),
        sa.Column("ocr_confidence", sa.Numeric(5, 4)),
        sa.Column("warnings", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _created(),
        sa.UniqueConstraint("extraction_id", "page_index", name="uq_file_pages_extraction_index"),
        sa.UniqueConstraint("id", "extraction_id", name="uq_file_pages_id_extraction"),
        sa.CheckConstraint("page_index > 0", name="ck_file_pages_index"),
        sa.CheckConstraint("extraction_method IN ('native', 'ocr', 'mixed', 'none')", name="ck_file_pages_method"),
        sa.CheckConstraint("outcome IN ('extracted', 'verified_blank', 'excluded_with_reason', 'failed', 'needs_review')", name="ck_file_pages_outcome"),
        sa.CheckConstraint("outcome <> 'excluded_with_reason' OR (exclusion_reason IS NOT NULL AND length(btrim(exclusion_reason)) > 0)", name="ck_file_pages_exclusion_reason"),
        sa.CheckConstraint("ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)", name="ck_file_pages_ocr_confidence"),
        sa.CheckConstraint("(width_pt IS NULL AND height_pt IS NULL) OR (width_pt IS NOT NULL AND height_pt IS NOT NULL AND width_pt > 0 AND height_pt > 0)", name="ck_file_pages_dimensions"),
        sa.CheckConstraint("rotation_deg IS NULL OR rotation_deg IN (0, 90, 180, 270)", name="ck_file_pages_rotation"),
    )
    op.create_table(
        "file_blocks",
        _id(),
        _uuid("extraction_id"),
        _uuid("page_id"),
        sa.Column("block_ordinal", sa.Integer(), nullable=False),
        sa.Column("block_kind", sa.String(20), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_sha256", sa.String(64)),
        sa.Column("page_char_start", sa.Integer()),
        sa.Column("page_char_end", sa.Integer()),
        sa.Column("bbox", JSONB()),
        sa.Column("warnings", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _created(),
        sa.ForeignKeyConstraint(["page_id", "extraction_id"], ["file_pages.id", "file_pages.extraction_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("page_id", "block_ordinal", name="uq_file_blocks_page_ordinal"),
        sa.UniqueConstraint("id", "page_id", "extraction_id", name="uq_file_blocks_id_page_extraction"),
        sa.CheckConstraint("block_ordinal > 0", name="ck_file_blocks_ordinal"),
        sa.CheckConstraint("block_kind IN ('heading', 'paragraph', 'table', 'footnote', 'note', 'header', 'footer', 'figure', 'other')", name="ck_file_blocks_kind"),
        sa.CheckConstraint("(page_char_start IS NULL AND page_char_end IS NULL) OR (page_char_start IS NOT NULL AND page_char_end IS NOT NULL AND page_char_start >= 0 AND page_char_end > page_char_start)", name="ck_file_blocks_page_range"),
    )
    op.create_table(
        "corpus_revisions",
        _id(),
        _fk("legal_version_id", "legal_versions.id"),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("normalizer_version", sa.String(100), nullable=False),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("review_state", sa.String(20), nullable=False, server_default="draft"),
        _fk("approved_by", "app_users.id", nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        _created(),
        sa.UniqueConstraint("legal_version_id", "revision_number", name="uq_corpus_revisions_version_number"),
        sa.UniqueConstraint("id", "legal_version_id", name="uq_corpus_revisions_id_version"),
        sa.CheckConstraint("revision_number > 0", name="ck_corpus_revisions_number"),
        sa.CheckConstraint("review_state IN ('draft', 'needs_review', 'approved', 'rejected')", name="ck_corpus_revisions_state"),
        sa.CheckConstraint("(review_state = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL AND content_sha256 IS NOT NULL) OR (review_state <> 'approved' AND approved_by IS NULL AND approved_at IS NULL)", name="ck_corpus_revisions_approval"),
    )
    op.create_table(
        "revision_sources",
        _uuid("revision_id"),
        _uuid("legal_version_id"),
        _uuid("extraction_id"),
        _uuid("file_id"),
        sa.Column("source_role", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.PrimaryKeyConstraint("revision_id", "extraction_id"),
        sa.ForeignKeyConstraint(["revision_id", "legal_version_id"], ["corpus_revisions.id", "corpus_revisions.legal_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["extraction_id", "file_id"], ["file_extractions.id", "file_extractions.file_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["file_id", "legal_version_id"], ["legal_file_assignments.file_id", "legal_file_assignments.legal_version_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("source_role IN ('primary', 'supplement', 'amendment')", name="ck_revision_sources_role"),
    )
    op.create_table(
        "provisions",
        _id(),
        _fk("revision_id", "corpus_revisions.id"),
        _uuid("parent_id", nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("reference_path", sa.Text()),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text()),
        sa.Column("own_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_sha256", sa.String(64)),
        _created(),
        sa.ForeignKeyConstraint(["parent_id", "revision_id"], ["provisions.id", "provisions.revision_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "revision_id", name="uq_provisions_id_revision"),
        sa.UniqueConstraint("revision_id", "ordinal", name="uq_provisions_revision_ordinal"),
        sa.UniqueConstraint("revision_id", "reference_path", name="uq_provisions_revision_path"),
        sa.CheckConstraint("ordinal > 0", name="ck_provisions_ordinal"),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_provisions_no_self_parent"),
        sa.CheckConstraint("kind IN ('preamble', 'part', 'chapter', 'section', 'subsection', 'paragraph', 'rule', 'schedule', 'article', 'other')", name="ck_provisions_kind"),
    )
    op.create_table(
        "provision_spans",
        _id(),
        _uuid("revision_id"),
        _uuid("provision_id"),
        _uuid("extraction_id"),
        _uuid("page_id"),
        _uuid("block_id", nullable=True),
        sa.Column("provision_char_start", sa.Integer(), nullable=False),
        sa.Column("provision_char_end", sa.Integer(), nullable=False),
        sa.Column("block_char_start", sa.Integer()),
        sa.Column("block_char_end", sa.Integer()),
        sa.Column("mapping_quality", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.ForeignKeyConstraint(["provision_id", "revision_id"], ["provisions.id", "provisions.revision_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revision_id", "extraction_id"], ["revision_sources.revision_id", "revision_sources.extraction_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["page_id", "extraction_id"], ["file_pages.id", "file_pages.extraction_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["block_id", "page_id", "extraction_id"], ["file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("provision_id", "provision_char_start", "provision_char_end", "page_id", "block_id", name="uq_provision_spans_range_source"),
        sa.CheckConstraint("provision_char_start >= 0 AND provision_char_end > provision_char_start", name="ck_provision_spans_provision_range"),
        sa.CheckConstraint("(block_char_start IS NULL AND block_char_end IS NULL) OR (block_id IS NOT NULL AND block_char_start IS NOT NULL AND block_char_end IS NOT NULL AND block_char_start >= 0 AND block_char_end > block_char_start)", name="ck_provision_spans_block_range"),
        sa.CheckConstraint("mapping_quality IN ('exact', 'approximate', 'page_only')", name="ck_provision_spans_quality"),
        sa.CheckConstraint("mapping_quality <> 'exact' OR (block_id IS NOT NULL AND block_char_start IS NOT NULL AND block_char_end IS NOT NULL)", name="ck_provision_spans_exact_source"),
    )
    op.create_table(
        "provision_references",
        _id(),
        _fk("source_provision_id", "provisions.id"),
        sa.Column("raw_reference", sa.Text(), nullable=False),
        sa.Column("relation_type", sa.String(50), nullable=False),
        _fk("target_instrument_id", "legal_instruments.id", nullable=True),
        sa.Column("target_reference_path", sa.Text()),
        sa.Column("resolution_status", sa.String(20), nullable=False, server_default="unresolved"),
        sa.Column("notes", sa.Text()),
        sa.CheckConstraint("resolution_status IN ('unresolved', 'resolved', 'ambiguous', 'rejected')", name="ck_provision_references_status"),
        sa.CheckConstraint("resolution_status <> 'resolved' OR (target_instrument_id IS NOT NULL AND target_reference_path IS NOT NULL)", name="ck_provision_references_resolved_target"),
    )
    op.create_table(
        "instrument_relations",
        _id(),
        _fk("from_instrument_id", "legal_instruments.id"),
        _fk("to_instrument_id", "legal_instruments.id"),
        sa.Column("relation_type", sa.String(20), nullable=False),
        _fk("source_provision_id", "provisions.id", nullable=True),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="proposed"),
        _fk("reviewed_by", "app_users.id", nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("source_note", sa.Text()),
        sa.CheckConstraint("from_instrument_id <> to_instrument_id", name="ck_instrument_relations_distinct"),
        sa.CheckConstraint("relation_type IN ('made_under', 'amends', 'repeals', 'supersedes', 'related_to')", name="ck_instrument_relations_type"),
        sa.CheckConstraint("review_status IN ('proposed', 'reviewed', 'rejected')", name="ck_instrument_relations_status"),
        sa.CheckConstraint("review_status <> 'reviewed' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="ck_instrument_relations_review_record"),
    )
    op.create_table(
        "review_events",
        _id(),
        _fk("actor_id", "app_users.id"),
        _fk("file_extraction_id", "file_extractions.id", nullable=True),
        _fk("corpus_revision_id", "corpus_revisions.id", nullable=True),
        _uuid("page_id", nullable=True),
        _uuid("block_id", nullable=True),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text()),
        _created(),
        sa.ForeignKeyConstraint(["page_id", "file_extraction_id"], ["file_pages.id", "file_pages.extraction_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["block_id", "page_id", "file_extraction_id"], ["file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("num_nonnulls(file_extraction_id, corpus_revision_id) = 1", name="ck_review_events_one_target"),
        sa.CheckConstraint("page_id IS NULL OR file_extraction_id IS NOT NULL", name="ck_review_events_page_target"),
        sa.CheckConstraint("block_id IS NULL OR page_id IS NOT NULL", name="ck_review_events_block_target"),
        sa.CheckConstraint("decision IN ('approve', 'reject', 'request_changes', 'accept', 'mark_excluded', 'mark_blank')", name="ck_review_events_decision"),
    )
    op.create_table(
        "processing_jobs",
        _id(),
        sa.Column("task_type", sa.String(20), nullable=False),
        _fk("target_file_id", "legal_files.id", nullable=True),
        _fk("target_extraction_id", "file_extractions.id", nullable=True),
        _fk("target_revision_id", "corpus_revisions.id", nullable=True),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("progress_total", sa.Integer()),
        sa.Column("progress_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        _created(),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_type", "idempotency_key", name="uq_processing_jobs_type_idempotency"),
        sa.CheckConstraint("num_nonnulls(target_file_id, target_extraction_id, target_revision_id) = 1", name="ck_processing_jobs_one_target"),
        sa.CheckConstraint("task_type IN ('extract', 'structure', 'verify')", name="ck_processing_jobs_type"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'cancelled')", name="ck_processing_jobs_status"),
        sa.CheckConstraint("progress_total IS NULL OR progress_total >= 0", name="ck_processing_jobs_total"),
        sa.CheckConstraint("progress_done >= 0 AND (progress_total IS NULL OR progress_done <= progress_total)", name="ck_processing_jobs_progress"),
    )
    op.create_table(
        "job_attempts",
        _id(),
        _fk("job_id", "processing_jobs.id"),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_number"),
        sa.CheckConstraint("attempt_number > 0", name="ck_job_attempts_number"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_job_attempts_status"),
    )

    for name, table, columns in (
        ("ix_legal_versions_instrument_id", "legal_versions", ["instrument_id"]),
        ("ix_file_extractions_file_id", "file_extractions", ["file_id"]),
        ("ix_legal_file_assignments_version_id", "legal_file_assignments", ["legal_version_id"]),
        ("ix_file_blocks_page_id", "file_blocks", ["page_id"]),
        ("ix_revision_sources_extraction_id", "revision_sources", ["extraction_id"]),
        ("ix_provisions_revision_parent", "provisions", ["revision_id", "parent_id"]),
        ("ix_provision_spans_provision_id", "provision_spans", ["provision_id"]),
        ("ix_provision_spans_page_id", "provision_spans", ["page_id"]),
        ("ix_provision_references_source", "provision_references", ["source_provision_id"]),
        ("ix_instrument_relations_from", "instrument_relations", ["from_instrument_id"]),
        ("ix_instrument_relations_to", "instrument_relations", ["to_instrument_id"]),
        ("ix_review_events_extraction", "review_events", ["file_extraction_id"]),
        ("ix_review_events_revision", "review_events", ["corpus_revision_id"]),
        ("ix_processing_jobs_status_lease", "processing_jobs", ["status", "lease_until"]),
        ("ix_job_attempts_job_id", "job_attempts", ["job_id"]),
    ):
        op.create_index(name, table, columns)

    # Review history is evidence, not mutable application state.
    op.execute("""
        CREATE FUNCTION legalmind_reject_review_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'review_events is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_review_events_append_only
        BEFORE UPDATE OR DELETE ON review_events
        FOR EACH ROW EXECUTE FUNCTION legalmind_reject_review_event_mutation()
    """)


def downgrade() -> None:
    # Explicit downgrade is destructive; this is provided for disposable local
    # databases. Never run against reviewed/published corpus data without a
    # backup and a deliberate migration plan.
    for table in (
        "job_attempts", "processing_jobs", "review_events",
        "instrument_relations", "provision_references", "provision_spans",
        "provisions", "revision_sources", "corpus_revisions", "file_blocks",
        "file_pages", "file_extractions", "legal_file_assignments",
        "legal_files", "legal_versions",
        "instrument_categories", "legal_instruments", "categories",
        "jurisdictions", "app_users",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION legalmind_reject_review_event_mutation()")
