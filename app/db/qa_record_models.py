"""Phase C audit records; no Phase A or Phase B table is changed."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QaRun(Base):
    __tablename__ = "qa_runs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    publication_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    reranker_model: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetrievalHit(Base):
    __tablename__ = "retrieval_hits"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    qa_run_id: Mapped[UUID] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    provision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    retrieval_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_rank: Mapped[int | None] = mapped_column(Integer)
    keyword_rank: Mapped[int | None] = mapped_column(Integer)
    vector_rank: Mapped[int | None] = mapped_column(Integer)
    rrf_rank: Mapped[int | None] = mapped_column(Integer)
    rrf_score: Mapped[float] = mapped_column(Float, nullable=False)
    reranker_rank: Mapped[int | None] = mapped_column(Integer)
    reranker_score: Mapped[float | None] = mapped_column(Float)


class AnswerClaim(Base):
    __tablename__ = "answer_claims"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    qa_run_id: Mapped[UUID] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(90), nullable=False)


class AnswerEvidence(Base):
    __tablename__ = "answer_evidence"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    answer_claim_id: Mapped[UUID] = mapped_column(ForeignKey("answer_claims.id", ondelete="CASCADE"), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(90), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    provision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    legal_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_span_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    file_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
