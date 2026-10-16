"""Create and activate validated corpus snapshots in one database transaction."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.legal_corpus_models import ChunkSet, CorpusRevision, LegalInstrument, LegalVersion
from app.db.publication_models import CorpusPublication, PublicationItem
from app.db.session import Database
from app.services.corpus_validation import PublicationValidationError, validate_corpus
from app.services.legal_chunking import ChunkConfig
from app.embeddings.encoder import get_tokenizer
from app.embeddings.profiles import ACTIVE_PROFILE

PUBLICATION_LOCK = 71091091


class PublicationRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    async def _lock(session):
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SET LOCAL statement_timeout = '120s'"))
        locked = await session.scalar(text(
            "SELECT pg_try_advisory_xact_lock(hashtext(current_schema()::text), :key)"),
            {"key": PUBLICATION_LOCK})
        if not locked:
            raise PublicationValidationError(["Another publication operation is in progress; retry when it finishes"])
        # Corpus publication is infrequent. A short SHARE lock prevents data
        # mutation between validation and sealing; SELECT/search remains free.
        try:
            await session.execute(text("""LOCK TABLE corpus_revisions, chunk_sets, legal_chunks,
                legal_chunk_provisions, legal_chunk_embeddings, embedding_profiles, provisions,
                provision_spans, revision_sources, file_extractions, file_pages, file_blocks,
                legal_files, legal_versions, legal_instruments IN SHARE MODE"""))
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise PublicationValidationError(["Corpus writers are busy; retry publication when they are idle"]) from exc
            raise

    async def plan(self) -> dict:
        """Return explicit choices; never silently activate every approved revision."""
        config = ChunkConfig()
        async with self.db.session() as session:
            rows = (await session.execute(select(ChunkSet.id, CorpusRevision.id.label("revision_id"),
                LegalInstrument.canonical_title, LegalVersion.edition_key)
                .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .where(CorpusRevision.review_state == "approved",
                       ChunkSet.content_sha256 == CorpusRevision.content_sha256,
                       ChunkSet.chunker_version == config.chunker_version,
                       ChunkSet.config_sha256 == config.config_sha256)
                .order_by(LegalInstrument.canonical_title, CorpusRevision.revision_number))).mappings().all()
        return {"chunk_set_ids": [str(r["id"]) for r in rows],
                "selection": [dict(r) for r in rows]}

    async def validate(self, chunk_set_ids):
        await asyncio.to_thread(get_tokenizer, ACTIVE_PROFILE)
        async with self.db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            return await validate_corpus(session, chunk_set_ids)

    async def create(self, name: str, chunk_set_ids: list[UUID], *, notes=None) -> dict:
        name = name.strip()
        if not name or len(name) > 160:
            raise ValueError("Publication name must contain 1 to 160 characters")
        await asyncio.to_thread(get_tokenizer, ACTIVE_PROFILE)
        async with self.db.session() as session, session.begin():
            await self._lock(session)
            report = await validate_corpus(session, chunk_set_ids)
            existing = await session.scalar(select(CorpusPublication).where(CorpusPublication.name == name))
            if existing:
                if existing.manifest_sha256 != report["manifest_sha256"]:
                    raise PublicationValidationError(["Publication name already pins a different manifest"])
                return self._summary(existing, created=False)
            publication = CorpusPublication(name=name, status="draft",
                profile_id=UUID(report["embedding_audit"]["profile_id"]),
                manifest_sha256=report["manifest_sha256"], chunk_count=report["chunk_count"],
                validation_report=report, notes=notes)
            session.add(publication)
            await session.flush()
            for item in report["items"]:
                session.add(PublicationItem(publication_id=publication.id,
                    **{f: UUID(item[f]) for f in ("chunk_set_id", "revision_id", "legal_version_id", "instrument_id")},
                    content_sha256=item["content_sha256"], chunk_count=item["chunk_count"]))
            await session.flush()
            publication.status = "ready"
            await session.flush()
            return self._summary(publication, created=True)

    async def activate(self, publication_id: UUID, *, expected_manifest_sha256=None) -> dict:
        await asyncio.to_thread(get_tokenizer, ACTIVE_PROFILE)
        async with self.db.session() as session, session.begin():
            await self._lock(session)
            publication = await session.get(CorpusPublication, publication_id)
            if publication is None:
                raise LookupError("Publication does not exist")
            if publication.status not in {"ready", "active", "retired"}:
                raise PublicationValidationError(["Publication has not been sealed"])
            if expected_manifest_sha256 is not None and publication.manifest_sha256 != expected_manifest_sha256:
                raise PublicationValidationError(["Manifest changed; inspect the intended publication"])
            items = (await session.scalars(select(PublicationItem).where(
                PublicationItem.publication_id == publication.id))).all()
            report = await validate_corpus(session, [i.chunk_set_id for i in items])
            if (report["manifest_sha256"] != publication.manifest_sha256 or
                report["chunk_count"] != publication.chunk_count or
                report["embedding_audit"]["profile_id"] != str(publication.profile_id)):
                raise PublicationValidationError(["Publication no longer matches its sealed validation manifest"])
            if publication.status == "active":
                return self._summary(publication, already_active=True)
            active = await session.scalar(select(CorpusPublication).where(CorpusPublication.status == "active"))
            if active:
                active.status = "retired"
                await session.flush()  # satisfy the single-active partial unique index
            publication.status = "active"
            publication.activated_at = datetime.now(UTC)
            await session.flush()
            return self._summary(publication, previous_publication_id=active.id if active else None)

    async def list(self):
        async with self.db.session() as session:
            rows = (await session.scalars(select(CorpusPublication).order_by(CorpusPublication.created_at.desc()))).all()
            return [self._summary(row) for row in rows]

    @staticmethod
    def _summary(publication, **extra):
        return {"publication_id": publication.id, "name": publication.name,
            "status": publication.status, "profile_id": publication.profile_id,
            "manifest_sha256": publication.manifest_sha256, "chunk_count": publication.chunk_count,
            "instrument_count": len(publication.validation_report.get("items", [])),
            "created_at": publication.created_at, "activated_at": publication.activated_at, **extra}
