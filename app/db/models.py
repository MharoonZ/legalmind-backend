"""Database tables for a versioned, review-gated legal corpus.

These classes describe the database shape for application code.  Alembic owns
the actual schema changes; importing this module does not create tables.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, Computed, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared SQLAlchemy metadata used by Alembic autogeneration."""


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


class LawPassage(Base):
    """One section fragment on one PDF page; its ID is the citation key."""

    __tablename__ = "law_passages"
    __table_args__ = (
        UniqueConstraint("section_id", "page_number", name="uq_law_passages_section_page"),
        Index("ix_law_passages_section_id", "section_id"),
        CheckConstraint("page_number > 0", name="ck_law_passages_page_positive"),
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_model IS NOT NULL)",
            name="ck_law_passages_embedding_pair",
        ),
        Index("ix_law_passages_search_vector", "search_vector", postgresql_using="gin"),
    )

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
    # 3,072 dimensions match the stack recommendation's embedding model.
    # Leave embeddings NULL until an explicit, model-versioned job fills them.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(3072))
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    section: Mapped[LawSection] = relationship(back_populates="passages")
