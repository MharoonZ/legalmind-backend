"""Legacy 30-section corpus and private contract tables.

These classes describe the database shape for application code.  Alembic owns
the actual schema changes; importing this module does not create tables. The
new full-PDF ingestion schema lives in ``app.db.legal_corpus_models``.

The embedding column width comes from `EMBEDDING_DIM` so the ORM, the search
SQL and the migration can never disagree about the vector size.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.db.base import Base

EMBEDDING_DIM = get_settings().embedding_dim

# pgvector can only build HNSW/IVFFlat indexes on vectors of 2000 dimensions
# or fewer. Above that the column still works, but every search is an exact scan.
HNSW_MAX_DIM = 2000


class LawSource(Base):
    """A frozen transcription of a particular source PDF.

    A correction produces a new corpus hash and thus a new row.  Approval is a
    separate human action, so extraction/import alone cannot make this text
    available to a user-facing search endpoint.
    """

    __tablename__ = "law_sources"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ('needs_review', 'approved', 'rejected')",
            name="ck_law_sources_review_status",
        ),
        CheckConstraint(
            "review_status <> 'approved' OR "
            "(reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_law_sources_approval_recorded",
        ),
        UniqueConstraint("corpus_sha256", name="uq_law_sources_corpus_sha256"),
    )

    id: Mapped[str] = mapped_column(String(72), primary_key=True)
    law_name: Mapped[str] = mapped_column(String(200), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="needs_review")
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sections: Mapped[list[LawSection]] = relationship(back_populates="source")


class LawSection(Base):
    """Complete section text, including its illustrations and page range."""

    __tablename__ = "law_sections"
    __table_args__ = (
        UniqueConstraint("source_id", "section_number", name="uq_law_sections_source_number"),
        Index("ix_law_sections_source_id", "source_id"),
        CheckConstraint("section_number BETWEEN 1 AND 30", name="ck_law_sections_sprint_range"),
        CheckConstraint(
            "pdf_page_start > 0 AND pdf_page_end >= pdf_page_start",
            name="ck_law_sections_pages",
        ),
    )

    id: Mapped[str] = mapped_column(String(76), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("law_sources.id", ondelete="RESTRICT"), nullable=False
    )
    section_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    pdf_page_end: Mapped[int] = mapped_column(Integer, nullable=False)

    source: Mapped[LawSource] = relationship(back_populates="sections")
    passages: Mapped[list[LawPassage]] = relationship(back_populates="section")


def _passage_table_args() -> tuple:
    args: list = [
        UniqueConstraint("section_id", "page_number", name="uq_law_passages_section_page"),
        Index("ix_law_passages_section_id", "section_id"),
        CheckConstraint("page_number > 0", name="ck_law_passages_page_positive"),
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_model IS NOT NULL)",
            name="ck_law_passages_embedding_pair",
        ),
        Index("ix_law_passages_search_vector", "search_vector", postgresql_using="gin"),
    ]
    if EMBEDDING_DIM <= HNSW_MAX_DIM:
        args.append(
            Index(
                "ix_law_passages_embedding_hnsw",
                "embedding",
                postgresql_using="hnsw",
                postgresql_with={"m": 16, "ef_construction": 64},
                postgresql_ops={"embedding": "vector_cosine_ops"},
            )
        )
    return tuple(args)


class LawPassage(Base):
    """One section fragment on one PDF page; its ID is the citation key."""

    __tablename__ = "law_passages"
    __table_args__ = _passage_table_args()

    evidence_id: Mapped[str] = mapped_column(String(67), primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("law_sections.id", ondelete="RESTRICT"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Postgres maintains this keyword index whenever search_text changes.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, search_text)", persisted=True),
        nullable=False,
    )
    # Filled only by scripts/build_embeddings.py, which also writes the model
    # name. Search never mixes vectors produced by two different models.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    section: Mapped[LawSection] = relationship(back_populates="passages")


# --------------------------------------------------------------------------
# Uploaded contracts (Sprint 2). Kept in their own tables, never joined with
# law_sources/law_sections/law_passages above (Sprint 2 shared rule 6): a
# user's uploaded contract is evidence to check against the law, never text
# the law-corpus search endpoint should ever be able to return.
#
# This is the *upload* stage only (Abdullah, Week 1). Haroon's clause
# extraction and the Week 2 AI summary are separate, later pieces of work;
# they should key off `DocumentVersion.id`, which is stable once created here.
# --------------------------------------------------------------------------

DOCUMENT_VERSION_STATUSES = (
    "uploaded",  # file stored, nothing processed yet
    "extracting",  # Haroon's stage: clauses/pages being extracted
    "extracted",  # Haroon's stage: done, ready for summarising
    "extraction_failed",
    "summarizing",  # Week 2: AI summary being generated
    "summarized",  # Week 2: done
    "summary_failed",
)


class Document(Base):
    """Identity of one uploaded contract file: what it is and where it lives.

    One row per upload. Re-processing (a retried extraction, a regenerated
    summary) creates a new `DocumentVersion`, not a new `Document`.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        CheckConstraint("page_count > 0", name="ck_documents_page_count_positive"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Local disk path for now (Sprint 2 scope: local/team-only demo). Swap for
    # object storage later without changing the upload API's response shape.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", order_by="DocumentVersion.version_number"
    )


class DocumentVersion(Base):
    """One processing attempt for a document: upload -> extraction -> summary.

    `status` only ever reaches "uploaded" from this sprint's upload code; the
    later statuses exist so Haroon's extraction and the Week 2 summary step
    can update the same row instead of needing a new table.
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
        Index("ix_document_versions_document_id", "document_id"),
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{status}'" for status in DOCUMENT_VERSION_STATUSES)
            + ")",
            name="ck_document_versions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")
    # The chat question the user typed alongside this upload, if any. Not
    # answerable until extraction (Haroon) and the AI step (Week 2) exist.
    question: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Recorded with every successful extraction so the same PDF can be
    # reproduced and compared after an extractor upgrade.
    extraction_engine: Mapped[str | None] = mapped_column(String(50))
    extraction_engine_version: Mapped[str | None] = mapped_column(String(50))
    extracted_text_sha256: Mapped[str | None] = mapped_column(String(64))
    extraction_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extraction_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    document: Mapped[Document] = relationship(back_populates="versions")
    pages: Mapped[list[ContractPage]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="ContractPage.page_number",
    )
    clauses: Mapped[list[ContractClause]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="ContractClause.ordinal",
    )


class ContractPage(Base):
    """Canonical selectable text for one page of an uploaded contract."""

    __tablename__ = "contract_pages"
    __table_args__ = (
        PrimaryKeyConstraint("document_version_id", "page_number"),
        CheckConstraint("page_number > 0", name="ck_contract_pages_page_positive"),
    )

    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    version: Mapped[DocumentVersion] = relationship(back_populates="pages")


class ContractClause(Base):
    """A deterministic, version-scoped clause with its original label."""

    __tablename__ = "contract_clauses"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "ordinal", name="uq_contract_clauses_version_ordinal"
        ),
        UniqueConstraint(
            "id", "document_version_id", name="uq_contract_clauses_id_version"
        ),
        Index("ix_contract_clauses_version_id", "document_version_id"),
        CheckConstraint("ordinal > 0", name="ck_contract_clauses_ordinal_positive"),
        CheckConstraint(
            "page_start > 0 AND page_end >= page_start",
            name="ck_contract_clauses_pages",
        ),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(100))
    heading: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)

    version: Mapped[DocumentVersion] = relationship(back_populates="clauses")
    spans: Mapped[list[ContractClauseSpan]] = relationship(
        back_populates="clause",
        cascade="all, delete-orphan",
        order_by="ContractClauseSpan.clause_char_start",
    )


class ContractClauseSpan(Base):
    """Maps a character range in a clause back to an original PDF page."""

    __tablename__ = "contract_clause_spans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clause_id", "document_version_id"],
            ["contract_clauses.id", "contract_clauses.document_version_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "page_number"],
            ["contract_pages.document_version_id", "contract_pages.page_number"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "clause_id",
            "page_number",
            "clause_char_start",
            "clause_char_end",
            name="uq_contract_clause_spans_range",
        ),
        Index("ix_contract_clause_spans_clause_id", "clause_id"),
        CheckConstraint("page_number > 0", name="ck_contract_clause_spans_page_positive"),
        CheckConstraint(
            "clause_char_start >= 0 AND clause_char_end > clause_char_start",
            name="ck_contract_clause_spans_chars",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clause_id: Mapped[str] = mapped_column(String(48), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(48), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    clause_char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    clause_char_end: Mapped[int] = mapped_column(Integer, nullable=False)

    clause: Mapped[ContractClause] = relationship(back_populates="spans")
