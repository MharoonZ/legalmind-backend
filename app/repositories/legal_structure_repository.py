"""Draft legal-structure revisions, source-safe provenance, and durable jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, func, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.legal_corpus_models import (
    AppUser, CorpusRevision, FileBlock, FileExtraction, FilePage, JobAttempt,
    InstrumentRelationCandidate, LegalFile, LegalFileAssignment, LegalInstrument, LegalVersion,
    ProcessingJob, Provision, ProvisionReference, ProvisionSpan,
    ReviewEvent, RevisionBlockCoverage, RevisionSource, SourceNoteMarkerLink,
)
from app.db.session import Database
from app.services.legal_structure import (
    NORMALIZER_VERSION, PARSER_VERSION, ParsedStructure, SourceBlock,
    SourceDocument, SourcePage,
)
from app.services.legal_extraction import sha256_text
from app.services.legal_audit import source_page_review_findings, structure_findings
from app.services.legal_relationships import extract_relation_candidates, normalize_title


class StructureConflict(Exception):
    """The requested edition, extraction, or revision is not ready."""


@dataclass(frozen=True, slots=True)
class StructureClaim:
    job_id: UUID
    attempt_id: UUID
    revision_id: UUID


def _chunks(items: list, size: int = 250):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class LegalStructureRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def approval_check(self, revision_id: UUID) -> dict | None:
        """Inspect the *persisted* revision before a local Phase B handoff decision."""
        audit = await self.audit(revision_id)
        if audit is None:
            return None
        async with self.db.session() as session:
            revision = await session.get(CorpusRevision, revision_id)
            latest_number = await session.scalar(select(func.max(CorpusRevision.revision_number)).where(
                CorpusRevision.legal_version_id == revision.legal_version_id
            ))
            job = (await session.execute(select(ProcessingJob).where(
                ProcessingJob.task_type == "structure",
                ProcessingJob.target_revision_id == revision_id,
            ))).scalar_one_or_none()
            sources = (await session.execute(select(RevisionSource).where(
                RevisionSource.revision_id == revision_id
            ))).scalars().all()
            pages = (await session.execute(select(FilePage, LegalFile.original_filename)
                .join(FileExtraction, FileExtraction.id == FilePage.extraction_id)
                .join(LegalFile, LegalFile.id == FileExtraction.file_id)
                .where(FilePage.extraction_id.in_([source.extraction_id for source in sources]))
                .order_by(LegalFile.original_filename, FilePage.page_index)
            )).all() if sources else []
            events = (await session.execute(select(ReviewEvent).where(
                ReviewEvent.file_extraction_id.in_([source.extraction_id for source in sources]),
                ReviewEvent.page_id.is_not(None),
            ).order_by(ReviewEvent.created_at.desc(), ReviewEvent.id.desc()))).scalars().all() if sources else []
            latest_page_decisions = {}
            for event in events:
                latest_page_decisions.setdefault(event.page_id, event.decision)
            page_review = [{
                "filename": filename, "page_index": page.page_index,
                "outcome": page.outcome, "decision": latest_page_decisions.get(page.id),
            } for page, filename in pages if page.outcome in {"needs_review", "failed"}]
            blockers = []
            if revision.review_state not in {"draft", "approved"}:
                blockers.append(f"Revision is {revision.review_state}, not a draft.")
            if revision.revision_number != latest_number:
                blockers.append("A newer persisted revision exists; approve the latest instead.")
            if job is None or job.status != "completed":
                blockers.append("Structure job is not completed.")
            if not revision.content_sha256:
                blockers.append("Revision has no completed content hash.")
            if not sources:
                blockers.append("Revision has no selected source extraction.")
            if not audit["provision_count"] or not audit["span_count"]:
                blockers.append("Revision has no structured provisions or source spans.")
            expected_pages = (await session.execute(select(RevisionSource.extraction_id, LegalFile.page_count)
                .join(LegalFile, LegalFile.id == RevisionSource.file_id)
                .where(RevisionSource.revision_id == revision_id))).all()
            page_counts = {}
            for page, _ in pages:
                page_counts[page.extraction_id] = page_counts.get(page.extraction_id, 0) + 1
            for extraction_id, expected in expected_pages:
                if page_counts.get(extraction_id, 0) != expected:
                    blockers.append("A selected extraction is missing physical page rows.")
                    break
            integrity_codes = {"missing_source_span", "span_text_mismatch", "uncovered_provision_text"}
            if any(item["code"] in integrity_codes for item in audit["findings"]):
                blockers.append("Exact source-text provenance failed the persisted audit.")
            if (audit["coverage_counts"].get("partial", 0)
                    or audit["coverage_counts"].get("unmapped", 0)):
                blockers.append("Some saved source blocks are partial or unmapped.")
            return {
                "revision_id": revision.id, "legal_version_id": revision.legal_version_id,
                "revision_number": revision.revision_number,
                "review_state": revision.review_state,
                "content_sha256": revision.content_sha256,
                "job_status": job.status if job else None,
                "is_latest": revision.revision_number == latest_number,
                "source_count": len(sources), "provision_count": audit["provision_count"],
                "span_count": audit["span_count"], "page_review": page_review,
                "findings": audit["findings"],
                "error_count": sum(item["severity"] == "error" for item in audit["findings"]),
                "warning_count": sum(item["severity"] != "error" for item in audit["findings"]),
                "hard_blockers": blockers, "can_approve": not blockers,
            }

    async def approve(self, revision_id: UUID, *, expected_content_sha256: str,
                      reason: str, acknowledge_findings: bool) -> dict | None:
        """Record an explicit local indexing decision; retain all audit exceptions."""
        reason = reason.strip()
        if len(reason) < 10 or len(reason) > 2000:
            raise StructureConflict("Give a review reason of 10–2000 characters.")
        check = await self.approval_check(revision_id)
        if check is None:
            return None
        if check["review_state"] == "approved":
            return {"revision_id": revision_id, "review_state": "approved", "already_approved": True}
        if check["hard_blockers"]:
            raise StructureConflict("Approval blocked: " + " ".join(check["hard_blockers"]))
        if expected_content_sha256 != check["content_sha256"]:
            raise StructureConflict("Revision changed; refresh the approval check.")
        if (check["findings"] or check["page_review"]) and not acknowledge_findings:
            raise StructureConflict("Acknowledge the displayed audit and page-review exceptions first.")
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            await session.execute(select(LegalVersion).where(
                LegalVersion.id == check["legal_version_id"]
            ).with_for_update())
            revision = (await session.execute(select(CorpusRevision).where(
                CorpusRevision.id == revision_id
            ).with_for_update())).scalar_one()
            if revision.review_state == "approved":
                return {"revision_id": revision_id, "review_state": "approved", "already_approved": True}
            latest_number = await session.scalar(select(func.max(CorpusRevision.revision_number)).where(
                CorpusRevision.legal_version_id == revision.legal_version_id
            ))
            if (revision.review_state != "draft" or revision.revision_number != latest_number
                    or revision.content_sha256 != expected_content_sha256):
                raise StructureConflict("Draft changed or a newer revision exists; refresh first.")
            await session.execute(pg_insert(AppUser).values(
                id=uuid4(), auth_provider="local_development",
                auth_subject="unauthenticated_corpus_approval",
                display_name="Local corpus approver (identity not verified)", role="user",
            ).on_conflict_do_nothing(index_elements=["auth_provider", "auth_subject"]))
            actor = (await session.execute(select(AppUser).where(
                AppUser.auth_provider == "local_development",
                AppUser.auth_subject == "unauthenticated_corpus_approval",
            ))).scalar_one()
            exceptions = sorted({item["code"] for item in check["findings"]})
            decision_reason = (f"Local Phase B indexing approval; reviewer identity not verified. "
                               f"Reason: {reason} Exceptions acknowledged: "
                               f"{', '.join(exceptions) or 'none'}; "
                               f"pages needing review: {len(check['page_review'])}.")
            revision.review_state = "approved"
            revision.approved_by = actor.id
            revision.approved_at = now
            session.add(ReviewEvent(
                id=uuid4(), actor_id=actor.id, corpus_revision_id=revision_id,
                decision="approve", reason=decision_reason,
            ))
        return {"revision_id": revision_id, "review_state": "approved",
                "already_approved": False, "acknowledged_error_count": check["error_count"],
                "acknowledged_warning_count": check["warning_count"],
                "acknowledged_page_count": len(check["page_review"])}

    async def source_options(self, version_id: UUID) -> list[dict]:
        async with self.db.session() as session:
            assignments = (await session.execute(
                select(LegalFileAssignment, LegalFile)
                .join(LegalFile, LegalFile.id == LegalFileAssignment.file_id)
                .where(LegalFileAssignment.legal_version_id == version_id)
                .order_by(LegalFileAssignment.created_at, LegalFile.id)
            )).all()
            if not assignments:
                return []
            file_ids = [source.id for _, source in assignments]
            extractions = (await session.execute(
                select(FileExtraction)
                .where(FileExtraction.file_id.in_(file_ids))
                .order_by(FileExtraction.created_at.desc(), FileExtraction.id.desc())
            )).scalars().all()
            latest: dict[UUID, FileExtraction] = {}
            for extraction in extractions:
                if extraction.processing_status == "completed":
                    latest.setdefault(extraction.file_id, extraction)
            return [{
                "file_id": source.id,
                "filename": source.original_filename,
                "page_count": source.page_count,
                "assignment_role": assignment.assignment_role,
                "extraction_id": latest[source.id].id if source.id in latest else None,
                "extraction_status": "completed" if source.id in latest else "missing",
            } for assignment, source in assignments]

    async def list_revisions(self, version_id: UUID) -> list[dict]:
        async with self.db.session() as session:
            rows = (await session.execute(
                select(CorpusRevision, ProcessingJob)
                .outerjoin(ProcessingJob, and_(
                    ProcessingJob.target_revision_id == CorpusRevision.id,
                    ProcessingJob.task_type == "structure",
                ))
                .where(CorpusRevision.legal_version_id == version_id)
                .order_by(CorpusRevision.revision_number.desc())
            )).all()
            return [{"id": revision.id, "revision_number": revision.revision_number,
                     "review_state": revision.review_state,
                     "parser_version": revision.parser_version,
                     "job_status": job.status if job else None,
                     "provision_count": (revision.parser_report or {}).get("coverage", {}).get("provisions")
                     if (revision.parser_report or {}).get("coverage") else None,
                     "created_at": revision.created_at}
                    for revision, job in rows]

    async def create_revision(self, version_id: UUID, extraction_ids: list[UUID] | None) -> dict:
        async with self.db.session() as session, session.begin():
            version = (await session.execute(
                select(LegalVersion).where(LegalVersion.id == version_id).with_for_update()
            )).scalar_one_or_none()
            if version is None:
                raise StructureConflict("Legal edition not found.")
            assignments = (await session.execute(
                select(LegalFileAssignment, LegalFile)
                .join(LegalFile, LegalFile.id == LegalFileAssignment.file_id)
                .where(LegalFileAssignment.legal_version_id == version_id)
                .order_by(LegalFileAssignment.created_at, LegalFile.id)
            )).all()
            if not assignments:
                raise StructureConflict("No PDFs are assigned to this edition.")
            chosen: list[tuple[LegalFileAssignment, LegalFile, FileExtraction]] = []
            requested = set(extraction_ids or [])
            if extraction_ids is not None and (not requested or len(requested) != len(extraction_ids)):
                raise StructureConflict("Select one or more distinct source extractions.")
            for assignment, source in assignments:
                rows = (await session.execute(
                    select(FileExtraction).where(
                        FileExtraction.file_id == source.id,
                        FileExtraction.processing_status == "completed",
                    ).order_by(FileExtraction.created_at.desc(), FileExtraction.id.desc())
                )).scalars().all()
                if extraction_ids is None:
                    if not rows:
                        raise StructureConflict(f"PDF {source.original_filename} has no completed extraction.")
                    chosen.append((assignment, source, rows[0]))
                else:
                    chosen.extend((assignment, source, item) for item in rows if item.id in requested)
            if extraction_ids is not None and {item.id for _, _, item in chosen} != requested:
                raise StructureConflict("A selected extraction is incomplete or not assigned to this edition.")
            if not chosen:
                raise StructureConflict("No completed source extraction was selected.")
            if len({source.id for _, source, _ in chosen}) != len(chosen):
                raise StructureConflict("Select only one extraction attempt per PDF.")
            blockers: list[str] = []
            selected_files = {source.id for _, source, _ in chosen}
            for _, source in assignments:
                if source.id not in selected_files:
                    blockers.append(f"Assigned PDF {source.original_filename} was omitted from this draft.")
            for _, source, extraction in chosen:
                pages = (await session.execute(
                    select(FilePage.id, FilePage.page_index, FilePage.outcome)
                    .where(FilePage.extraction_id == extraction.id)
                    .order_by(FilePage.page_index)
                )).all()
                if [index for _, index, _ in pages] != list(range(1, source.page_count + 1)):
                    raise StructureConflict(f"Extraction {extraction.id} is missing physical pages.")
                events = (await session.execute(select(ReviewEvent).where(
                    ReviewEvent.file_extraction_id == extraction.id,
                    ReviewEvent.page_id.is_not(None),
                ).order_by(ReviewEvent.created_at.desc(), ReviewEvent.id.desc()))).scalars().all()
                latest_decision = {}
                for event in events:
                    latest_decision.setdefault(event.page_id, event.decision)
                for page_id, index, outcome in pages:
                    if outcome == "needs_review" and latest_decision.get(page_id) == "mark_blank":
                        continue
                    if outcome in {"failed", "needs_review", "excluded_with_reason"}:
                        blockers.append(f"{source.original_filename} page {index}: {outcome}")
            revision_number = (await session.scalar(
                select(func.coalesce(func.max(CorpusRevision.revision_number), 0))
                .where(CorpusRevision.legal_version_id == version_id)
            )) + 1
            revision = CorpusRevision(
                id=uuid4(), legal_version_id=version_id, revision_number=revision_number,
                parser_version=PARSER_VERSION, normalizer_version=NORMALIZER_VERSION,
                review_state="draft",
                parser_report={"warnings": [{"code": "source_requires_review", "message": value}
                                            for value in blockers], "coverage": None},
            )
            session.add(revision)
            await session.flush()
            session.add_all(RevisionSource(
                revision_id=revision.id, legal_version_id=version_id,
                extraction_id=extraction.id, file_id=source.id,
                source_role=assignment.assignment_role,
            ) for assignment, source, extraction in chosen)
            job = ProcessingJob(
                id=uuid4(), task_type="structure", target_revision_id=revision.id,
                idempotency_key=f"structure:{revision.id}", status="queued",
                progress_total=sum(source.page_count for _, source, _ in chosen),
            )
            session.add(job)
            await session.flush()
            return {"revision_id": revision.id, "revision_number": revision_number,
                    "job_id": job.id, "job_status": job.status,
                    "source_count": len(chosen), "source_warnings": blockers}

    async def claim_next(self, *, worker_id: str, lease_seconds: int = 3600) -> StructureClaim | None:
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.task_type == "structure",
                    or_(ProcessingJob.status == "queued", and_(
                        ProcessingJob.status == "running",
                        or_(ProcessingJob.lease_until < now, ProcessingJob.lease_until.is_(None)),
                    )),
                ).order_by(ProcessingJob.created_at).limit(1).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if job is None:
                return None
            if job.status == "running":
                previous = (await session.execute(
                    select(JobAttempt).where(JobAttempt.job_id == job.id)
                    .order_by(JobAttempt.attempt_number.desc()).limit(1)
                )).scalar_one_or_none()
                if previous and previous.status == "running":
                    previous.status = "failed"
                    previous.error_message = "Worker lease expired; a new attempt took over."
                    previous.completed_at = now
            revision = await session.get(CorpusRevision, job.target_revision_id)
            if revision is None or revision.review_state != "draft":
                raise StructureConflict("A structure job requires an existing draft revision.")
            existing = await session.scalar(select(func.count(Provision.id)).where(Provision.revision_id == revision.id))
            if existing:
                raise StructureConflict("Draft already contains provisions; create a new revision for reparsing.")
            number = (await session.scalar(select(func.coalesce(func.max(JobAttempt.attempt_number), 0))
                                           .where(JobAttempt.job_id == job.id))) + 1
            attempt = JobAttempt(id=uuid4(), job_id=job.id, attempt_number=number,
                                 worker_id=worker_id, status="running", started_at=now,
                                 heartbeat_at=now)
            session.add(attempt)
            job.status = "running"
            job.started_at = now
            job.completed_at = None
            job.error_message = None
            job.lease_until = now + timedelta(seconds=lease_seconds)
            job.progress_done = 0
            await session.flush()
            return StructureClaim(job.id, attempt.id, revision.id)

    async def load_sources(self, revision_id: UUID) -> list[SourceDocument]:
        async with self.db.session() as session:
            sources = (await session.execute(
                select(RevisionSource, LegalFile, LegalInstrument)
                .join(LegalFile, LegalFile.id == RevisionSource.file_id)
                .join(CorpusRevision, CorpusRevision.id == RevisionSource.revision_id)
                .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
                .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
                .where(RevisionSource.revision_id == revision_id)
                .order_by(case(
                    (RevisionSource.source_role == "primary", 0),
                    (RevisionSource.source_role == "supplement", 1),
                    else_=2,
                ), LegalFile.id)
            )).all()
            if not sources:
                raise StructureConflict("Draft has no source extractions.")
            extraction_ids = [source.extraction_id for source, _, _ in sources]
            pages = (await session.execute(
                select(FilePage).where(FilePage.extraction_id.in_(extraction_ids))
                .order_by(FilePage.extraction_id, FilePage.page_index)
            )).scalars().all()
            review_events = (await session.execute(select(ReviewEvent).where(
                ReviewEvent.file_extraction_id.in_(extraction_ids),
                ReviewEvent.page_id.is_not(None),
            ).order_by(ReviewEvent.created_at.desc(), ReviewEvent.id.desc()))).scalars().all()
            latest_review = {}
            for event in review_events:
                latest_review.setdefault(event.page_id, event)
            blocks = (await session.execute(
                select(FileBlock).where(FileBlock.extraction_id.in_(extraction_ids))
                .order_by(FileBlock.extraction_id, FileBlock.page_id, FileBlock.block_ordinal)
            )).scalars().all()
            blocks_by_page: dict[UUID, list[FileBlock]] = {}
            for block in blocks:
                blocks_by_page.setdefault(block.page_id, []).append(block)
            pages_by_extraction: dict[UUID, list[FilePage]] = {}
            for page in pages:
                pages_by_extraction.setdefault(page.extraction_id, []).append(page)
            documents = []
            for source, file, instrument in sources:
                source_pages = []
                for page in pages_by_extraction.get(source.extraction_id, []):
                    blank_reviewed = (page.id in latest_review
                                      and latest_review[page.id].decision == "mark_blank"
                                      and any(item.get("code") == "blank_candidate"
                                              for item in page.warnings or []))
                    source_blocks = tuple(SourceBlock(
                        id=block.id, page_id=page.id, extraction_id=source.extraction_id,
                        file_id=file.id, page_index=page.page_index,
                        ordinal=block.block_ordinal, kind=block.block_kind,
                        text=block.normalized_text, bbox=block.bbox,
                        page_height=float(page.height_pt) if page.height_pt else None,
                    ) for block in blocks_by_page.get(page.id, []))
                    source_pages.append(SourcePage(
                        id=page.id, extraction_id=source.extraction_id, file_id=file.id,
                        page_index=page.page_index,
                        outcome="verified_blank" if blank_reviewed else page.outcome,
                        warnings=[item for item in page.warnings
                                  if not (blank_reviewed and item.get("code") == "blank_candidate")],
                        height_pt=float(page.height_pt) if page.height_pt else None,
                        blocks=source_blocks,
                    ))
                documents.append(SourceDocument(
                    file_id=file.id, extraction_id=source.extraction_id,
                    title=instrument.canonical_title, instrument_type=instrument.instrument_type,
                    page_count=file.page_count, pages=tuple(source_pages),
                ))
            return documents

    async def complete(self, claim: StructureClaim, parsed: ParsedStructure,
                       documents: list[SourceDocument]) -> None:
        source_blocks = {block.id: block for document in documents
                         for page in document.pages for block in page.blocks}
        nodes = {node.id: node for node in parsed.nodes}
        if len(nodes) != len(parsed.nodes) or len({n.reference_path for n in parsed.nodes}) != len(parsed.nodes):
            raise StructureConflict("Duplicate provision identity or reference path.")
        if len(parsed.decisions) != len(source_blocks):
            raise StructureConflict("Source-block coverage is incomplete.")
        source_ranges: dict[UUID, list[tuple[int, int]]] = {}
        for span in parsed.spans:
            node, block = nodes.get(span.provision_id), source_blocks.get(span.block_id)
            if node is None or block is None or span.extraction_id != block.extraction_id or span.page_id != block.page_id:
                raise StructureConflict("A provision span points outside its selected source.")
            if not (0 <= span.provision_char_start < span.provision_char_end <= len(node.own_text)
                    and 0 <= span.block_char_start < span.block_char_end <= len(block.text)):
                raise StructureConflict("A provision span has invalid character offsets.")
            if node.own_text[span.provision_char_start:span.provision_char_end] != block.text[span.block_char_start:span.block_char_end]:
                raise StructureConflict("An exact provision span differs from stored source text.")
            source_ranges.setdefault(block.id, []).append((span.block_char_start, span.block_char_end))
        for ranges in source_ranges.values():
            previous_end = -1
            for start, end in sorted(ranges):
                if start < previous_end:
                    raise StructureConflict("Overlapping source spans would duplicate text.")
                previous_end = end
        note_source_extractions = {(span.provision_id, span.extraction_id) for span in parsed.spans}
        marker_keys: set[tuple[UUID, int, int]] = set()
        for link in parsed.note_links:
            note, target = nodes.get(link.note_provision_id), nodes.get(link.target_provision_id)
            block = source_blocks.get(link.block_id)
            if (note is None or note.kind != "other" or note.label != link.marker_label
                    or not (note.heading or "").startswith("Source note ")):
                raise StructureConflict("A source-note marker has no matching note provision.")
            if link.target_provision_id is not None and (target is None or target.id == note.id):
                raise StructureConflict("A source-note marker targets a missing or identical provision.")
            if (block is None or link.extraction_id != block.extraction_id
                    or link.page_id != block.page_id):
                raise StructureConflict("A source-note marker points outside its selected source.")
            if (note.id, link.extraction_id) not in note_source_extractions:
                raise StructureConflict("A source-note marker is not backed by note text from the same PDF.")
            if (not link.marker_label.isascii() or not link.marker_label.isdecimal()
                    or not (0 <= link.block_char_start < link.block_char_end <= len(block.text))
                    or block.text[link.block_char_start:link.block_char_end] != link.marker_label):
                raise StructureConflict("A source-note marker does not match exact saved source text.")
            key = (block.id, link.block_char_start, link.block_char_end)
            if key in marker_keys:
                raise StructureConflict("A source-note marker link is duplicated.")
            marker_keys.add(key)
        span_ids = {span: uuid4() for span in parsed.spans}
        if len(span_ids) != len(parsed.spans):
            raise StructureConflict("A draft contains duplicate exact source spans.")
        relation_candidates = extract_relation_candidates(
            parsed, source_titles={document.title for document in documents},
        )
        for candidate in relation_candidates:
            node = nodes.get(candidate.source_provision_id)
            span = candidate.source_span
            block = source_blocks.get(span.block_id)
            if (node is None or span.provision_id != node.id or span not in span_ids
                    or block is None):
                raise StructureConflict("A proposed instrument relationship lacks a selected exact span.")
            if not (span.provision_char_start <= candidate.source_char_start
                    < candidate.source_char_end <= span.provision_char_end):
                raise StructureConflict("A proposed instrument relationship exceeds its exact span.")
            offset = candidate.source_char_start - span.provision_char_start
            block_start = span.block_char_start + offset
            block_end = block_start + len(candidate.evidence_text)
            if (node.own_text[candidate.source_char_start:candidate.source_char_end]
                    != candidate.evidence_text
                    or block.text[block_start:block_end] != candidate.evidence_text):
                raise StructureConflict("A proposed instrument relationship differs from stored PDF text.")
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(select(ProcessingJob)
                   .where(ProcessingJob.id == claim.job_id).with_for_update())).scalar_one()
            attempt = await session.get(JobAttempt, claim.attempt_id)
            revision = await session.get(CorpusRevision, claim.revision_id)
            if (job.status != "running" or attempt is None or attempt.status != "running"
                    or revision is None or revision.review_state != "draft"):
                raise StructureConflict("Structure job is no longer owned by this attempt.")
            if await session.scalar(select(func.count(Provision.id)).where(Provision.revision_id == claim.revision_id)):
                raise StructureConflict("A draft already has structure; reparsing requires a new revision.")
            for batch in _chunks(parsed.nodes):
                await session.execute(insert(Provision).values([{
                    "id": n.id, "revision_id": claim.revision_id, "parent_id": n.parent_id,
                    "kind": n.kind, "label": n.label, "reference_path": n.reference_path,
                    "ordinal": n.ordinal, "heading": n.heading, "own_text": n.own_text,
                    "source_status": n.source_status,
                    "text_sha256": sha256_text(n.own_text),
                } for n in batch]))
            for batch in _chunks(parsed.spans):
                await session.execute(insert(ProvisionSpan).values([{
                    "id": span_ids[s], "revision_id": claim.revision_id,
                    "provision_id": s.provision_id, "extraction_id": s.extraction_id,
                    "page_id": s.page_id, "block_id": s.block_id,
                    "provision_char_start": s.provision_char_start,
                    "provision_char_end": s.provision_char_end,
                    "block_char_start": s.block_char_start, "block_char_end": s.block_char_end,
                    "mapping_quality": "exact",
                } for s in batch]))
            if relation_candidates:
                source_version = await session.get(LegalVersion, revision.legal_version_id)
                if source_version is None:
                    raise StructureConflict("The draft legal edition disappeared.")
                instruments = (await session.execute(
                    select(LegalInstrument.id, LegalInstrument.canonical_title)
                )).all()
                by_title: dict[str, set[UUID]] = {}
                for instrument_id, title in instruments:
                    if instrument_id != source_version.instrument_id:
                        by_title.setdefault(normalize_title(title), set()).add(instrument_id)
                for batch in _chunks(relation_candidates):
                    await session.execute(insert(InstrumentRelationCandidate).values([{
                        "id": uuid4(), "revision_id": claim.revision_id,
                        "source_provision_id": item.source_provision_id,
                        "evidence_span_id": span_ids[item.source_span],
                        "source_char_start": item.source_char_start,
                        "source_char_end": item.source_char_end,
                        "evidence_text": item.evidence_text,
                        "target_title": item.target_title,
                        "target_reference": item.target_reference,
                        "candidate_instrument_id": next(iter(matches)) if len(matches) == 1 else None,
                        "candidate_version_id": None,
                        "relation_kind": item.relation_kind,
                        "link_basis": item.link_basis,
                        "status": "proposed" if len(matches) == 1 else "unresolved",
                    } for item in batch
                      for matches in (by_title.get(normalize_title(item.target_title), set()),)]))
            for batch in _chunks(parsed.note_links):
                await session.execute(insert(SourceNoteMarkerLink).values([{
                    "id": uuid4(), "revision_id": claim.revision_id,
                    "note_provision_id": link.note_provision_id,
                    "target_provision_id": link.target_provision_id,
                    "marker_label": link.marker_label,
                    "extraction_id": link.extraction_id,
                    "page_id": link.page_id,
                    "block_id": link.block_id,
                    "block_char_start": link.block_char_start,
                    "block_char_end": link.block_char_end,
                } for link in batch]))
            for batch in _chunks(parsed.references):
                await session.execute(insert(ProvisionReference).values([{
                    "id": uuid4(), "source_provision_id": ref.provision_id,
                    "raw_reference": ref.raw_reference, "relation_type": "textual_mention",
                    "resolution_status": "unresolved",
                } for ref in batch]))
            for batch in _chunks(parsed.decisions):
                await session.execute(insert(RevisionBlockCoverage).values([{
                    "id": uuid4(), "revision_id": claim.revision_id,
                    "extraction_id": decision.extraction_id,
                    "page_id": decision.page_id, "block_id": decision.block_id,
                    "status": decision.status, "reason": decision.reason,
                } for decision in batch]))
            revision.content_sha256 = parsed.content_hash()
            creation_warnings = (revision.parser_report or {}).get("warnings", [])
            revision.parser_report = {"warnings": creation_warnings + parsed.warnings, "coverage": parsed.coverage,
                                      "parser_version": PARSER_VERSION}
            job.progress_done = job.progress_total or parsed.coverage["recorded_pages"]
            job.status = "completed"
            job.lease_until = None
            job.completed_at = now
            attempt.status = "completed"
            attempt.heartbeat_at = now
            attempt.completed_at = now

    async def fail(self, claim: StructureClaim, message: str) -> None:
        now = datetime.now(UTC)
        async with self.db.session() as session, session.begin():
            job = (await session.execute(select(ProcessingJob)
                   .where(ProcessingJob.id == claim.job_id).with_for_update())).scalar_one()
            attempt = await session.get(JobAttempt, claim.attempt_id)
            if job.status != "running" or attempt is None or attempt.status != "running":
                return
            job.status = "failed"
            job.error_message = message[:2000]
            job.lease_until = None
            job.completed_at = now
            attempt.status = "failed"
            attempt.error_message = message[:2000]
            attempt.completed_at = now

    async def retry(self, revision_id: UUID) -> bool:
        async with self.db.session() as session, session.begin():
            revision = await session.get(CorpusRevision, revision_id)
            if revision is None or revision.review_state != "draft":
                return False
            job = (await session.execute(select(ProcessingJob).where(
                ProcessingJob.task_type == "structure",
                ProcessingJob.target_revision_id == revision_id,
            ).with_for_update())).scalar_one_or_none()
            if job is None or job.status != "failed":
                return False
            if await session.scalar(select(func.count(Provision.id)).where(Provision.revision_id == revision_id)):
                return False
            job.status = "queued"
            job.error_message = None
            job.lease_until = None
            job.started_at = None
            job.completed_at = None
            job.progress_done = 0
            return True

    async def overview(self, revision_id: UUID) -> dict | None:
        async with self.db.session() as session:
            revision = await session.get(CorpusRevision, revision_id)
            if revision is None:
                return None
            version = await session.get(LegalVersion, revision.legal_version_id)
            instrument = await session.get(LegalInstrument, version.instrument_id)
            job = (await session.execute(select(ProcessingJob).where(
                ProcessingJob.task_type == "structure",
                ProcessingJob.target_revision_id == revision_id,
            ))).scalar_one_or_none()
            sources = (await session.execute(select(RevisionSource, LegalFile)
                .join(LegalFile, LegalFile.id == RevisionSource.file_id)
                .where(RevisionSource.revision_id == revision_id)
                .order_by(RevisionSource.source_role, LegalFile.id))).all()
            nodes = (await session.execute(select(Provision)
                .where(Provision.revision_id == revision_id)
                .order_by(Provision.ordinal))).scalars().all()
            counts = dict((await session.execute(select(ProvisionSpan.provision_id, func.count(ProvisionSpan.id))
                .where(ProvisionSpan.revision_id == revision_id)
                .group_by(ProvisionSpan.provision_id))).all())
            return {
                "id": revision.id, "revision_number": revision.revision_number,
                "legal_version_id": revision.legal_version_id,
                "title": instrument.canonical_title, "edition_key": version.edition_key,
                "review_state": revision.review_state,
                "parser_version": revision.parser_version,
                "content_sha256": revision.content_sha256,
                "parser_report": revision.parser_report,
                "job": {"id": job.id, "status": job.status, "progress_done": job.progress_done,
                        "progress_total": job.progress_total, "error_message": job.error_message} if job else None,
                "sources": [{"file_id": file.id, "extraction_id": source.extraction_id,
                             "filename": file.original_filename, "role": source.source_role,
                             "page_count": file.page_count} for source, file in sources],
                "provisions": [{"id": node.id, "parent_id": node.parent_id,
                                "kind": node.kind, "label": node.label,
                                "reference_path": node.reference_path,
                                "ordinal": node.ordinal, "heading": node.heading,
                                "source_status": node.source_status,
                                "text_length": len(node.own_text),
                                "span_count": counts.get(node.id, 0)} for node in nodes],
            }

    async def relationship_candidates(self, revision_id: UUID) -> dict | None:
        """Read-only proposals; IDs and relation words are not verified law."""
        async with self.db.session() as session:
            if await session.get(CorpusRevision, revision_id) is None:
                return None
            rows = (await session.execute(
                select(InstrumentRelationCandidate, Provision, ProvisionSpan,
                       FilePage, FileExtraction)
                .join(Provision, Provision.id == InstrumentRelationCandidate.source_provision_id)
                .join(ProvisionSpan, ProvisionSpan.id == InstrumentRelationCandidate.evidence_span_id)
                .join(FilePage, FilePage.id == ProvisionSpan.page_id)
                .join(FileExtraction, FileExtraction.id == ProvisionSpan.extraction_id)
                .where(InstrumentRelationCandidate.revision_id == revision_id)
                .order_by(Provision.ordinal, InstrumentRelationCandidate.source_char_start)
            )).all()
            candidates = []
            for item, node, span, page, extraction in rows:
                block_start = (span.block_char_start + item.source_char_start
                               - span.provision_char_start)
                candidates.append({
                    "id": item.id, "relation_kind": item.relation_kind,
                    "status": item.status, "link_basis": item.link_basis,
                    "source_provision_id": node.id,
                    "source_reference_path": node.reference_path,
                    "evidence_span_id": span.id,
                    "file_id": extraction.file_id, "page_index": page.page_index,
                    "block_id": span.block_id,
                    "source_char_start": item.source_char_start,
                    "source_char_end": item.source_char_end,
                    "block_char_start": block_start,
                    "block_char_end": block_start + len(item.evidence_text),
                    "evidence_text": item.evidence_text,
                    "target_title": item.target_title,
                    "target_reference": item.target_reference,
                    "candidate_instrument_id": item.candidate_instrument_id,
                    "candidate_version_id": item.candidate_version_id,
                })
            return {
                "revision_id": revision_id,
                "notice": "Automatically proposed from draft source text. Not verified legal effect, current applicability, or resolved target provisions.",
                "candidates": candidates,
            }

    async def provision_detail(self, revision_id: UUID, provision_id: UUID) -> dict | None:
        async with self.db.session() as session:
            node = (await session.execute(select(Provision).where(
                Provision.id == provision_id, Provision.revision_id == revision_id
            ))).scalar_one_or_none()
            if node is None:
                return None
            spans = (await session.execute(select(ProvisionSpan, FilePage, FileBlock, FileExtraction)
                .join(FilePage, FilePage.id == ProvisionSpan.page_id)
                .join(FileBlock, FileBlock.id == ProvisionSpan.block_id)
                .join(FileExtraction, FileExtraction.id == ProvisionSpan.extraction_id)
                .where(ProvisionSpan.provision_id == provision_id)
                .order_by(ProvisionSpan.provision_char_start))).all()
            references = (await session.execute(select(ProvisionReference)
                .where(ProvisionReference.source_provision_id == provision_id))).scalars().all()
            note_link_rows = (await session.execute(
                select(SourceNoteMarkerLink, FileBlock, FilePage)
                .join(FileBlock, FileBlock.id == SourceNoteMarkerLink.block_id)
                .join(FilePage, FilePage.id == SourceNoteMarkerLink.page_id)
                .where(SourceNoteMarkerLink.revision_id == revision_id,
                       or_(SourceNoteMarkerLink.note_provision_id == provision_id,
                           SourceNoteMarkerLink.target_provision_id == provision_id))
                .order_by(FilePage.page_index, FileBlock.block_ordinal,
                          SourceNoteMarkerLink.block_char_start)
            )).all()
            linked_ids = {linked_id for link, _, _ in note_link_rows
                          for linked_id in (link.note_provision_id, link.target_provision_id)
                          if linked_id is not None}
            linked_nodes = {item.id: item for item in (await session.execute(
                select(Provision).where(Provision.revision_id == revision_id,
                                        Provision.id.in_(linked_ids))
            )).scalars().all()} if linked_ids else {}
            return {
                "id": node.id, "kind": node.kind, "label": node.label,
                "reference_path": node.reference_path, "heading": node.heading,
                "own_text": node.own_text,
                "source_status": node.source_status,
                "spans": [{"file_id": extraction.file_id, "extraction_id": span.extraction_id,
                           "page_index": page.page_index, "page_id": page.id,
                           "block_id": block.id, "block_ordinal": block.block_ordinal,
                           "provision_char_start": span.provision_char_start,
                           "provision_char_end": span.provision_char_end,
                           "block_char_start": span.block_char_start,
                           "block_char_end": span.block_char_end,
                           "mapping_quality": span.mapping_quality,
                           "source_text": block.normalized_text[span.block_char_start:span.block_char_end]
                           if span.block_char_start is not None else ""}
                          for span, page, block, extraction in spans],
                "references": [{"raw_reference": ref.raw_reference,
                                "resolution_status": ref.resolution_status}
                               for ref in references],
                "note_links": [{
                    "id": link.id, "marker_label": link.marker_label,
                    "marker_text": block.normalized_text[link.block_char_start:link.block_char_end],
                    "extraction_id": link.extraction_id, "page_index": page.page_index,
                    "page_id": page.id, "block_id": block.id,
                    "block_char_start": link.block_char_start,
                    "block_char_end": link.block_char_end,
                    "note_provision_id": link.note_provision_id,
                    "note_reference_path": linked_nodes[link.note_provision_id].reference_path,
                    "note_text": linked_nodes[link.note_provision_id].own_text,
                    "target_provision_id": link.target_provision_id,
                    "target_reference_path": linked_nodes[link.target_provision_id].reference_path
                    if link.target_provision_id is not None else None,
                    "target_kind": linked_nodes[link.target_provision_id].kind
                    if link.target_provision_id is not None else None,
                    "target_label": linked_nodes[link.target_provision_id].label
                    if link.target_provision_id is not None else None,
                } for link, block, page in note_link_rows],
            }

    async def coverage(self, revision_id: UUID, *, status: str | None = None,
                       limit: int = 100, offset: int = 0) -> list[dict] | None:
        async with self.db.session() as session:
            if await session.get(CorpusRevision, revision_id) is None:
                return None
            query = (select(RevisionBlockCoverage, FilePage, FileBlock, FileExtraction)
                     .join(FilePage, FilePage.id == RevisionBlockCoverage.page_id)
                     .join(FileBlock, FileBlock.id == RevisionBlockCoverage.block_id)
                     .join(FileExtraction, FileExtraction.id == RevisionBlockCoverage.extraction_id)
                     .where(RevisionBlockCoverage.revision_id == revision_id))
            if status:
                query = query.where(RevisionBlockCoverage.status == status)
            rows = (await session.execute(query.order_by(
                FileExtraction.file_id, FilePage.page_index, FileBlock.block_ordinal
            ).offset(offset).limit(limit))).all()
            return [{"file_id": extraction.file_id, "extraction_id": extraction.id,
                     "page_index": page.page_index, "block_id": block.id,
                     "block_ordinal": block.block_ordinal, "status": decision.status,
                     "reason": decision.reason, "text_preview": block.normalized_text[:300]}
                    for decision, page, block, extraction in rows]

    async def audit(self, revision_id: UUID) -> dict | None:
        """Check persisted text and source mappings, never the parser's in-memory result."""
        async with self.db.session() as session:
            revision = await session.get(CorpusRevision, revision_id)
            if revision is None:
                return None
            instrument_type = await session.scalar(
                select(LegalInstrument.instrument_type)
                .join(LegalVersion, LegalVersion.instrument_id == LegalInstrument.id)
                .where(LegalVersion.id == revision.legal_version_id)
            )
            nodes = (await session.execute(select(Provision).where(
                Provision.revision_id == revision_id).order_by(Provision.ordinal)
            )).scalars().all()
            span_rows = (await session.execute(
                select(ProvisionSpan, FileBlock)
                .join(FileBlock, FileBlock.id == ProvisionSpan.block_id)
                .where(ProvisionSpan.revision_id == revision_id)
            )).all()
            sources = (await session.execute(select(RevisionSource).where(
                RevisionSource.revision_id == revision_id
            ))).scalars().all()
            primary_ids = [source.extraction_id for source in sources
                           if source.source_role == "primary"]
            source_ids = primary_ids or [source.extraction_id for source in sources]
            source_blocks = (await session.execute(
                select(FileBlock.id, FileBlock.page_id, FileBlock.extraction_id,
                       FileBlock.block_kind, FileBlock.normalized_text,
                       FilePage.page_index)
                .join(FilePage, FilePage.id == FileBlock.page_id)
                .where(FileBlock.extraction_id.in_(source_ids))
            )).all() if source_ids else []
            source_pages = (await session.execute(
                select(FilePage.id, LegalFile.original_filename, FilePage.page_index,
                       FilePage.outcome, FilePage.warnings,
                       FilePage.table_regions)
                .join(FileExtraction, FileExtraction.id == FilePage.extraction_id)
                .join(LegalFile, LegalFile.id == FileExtraction.file_id)
                .where(FilePage.extraction_id.in_(source_ids))
            )).all() if source_ids else []
            review_events = (await session.execute(select(ReviewEvent).where(
                ReviewEvent.file_extraction_id.in_(source_ids),
                ReviewEvent.page_id.is_not(None),
            ).order_by(ReviewEvent.created_at.desc(), ReviewEvent.id.desc()))).scalars().all() if source_ids else []
            latest_review = {}
            for event in review_events:
                latest_review.setdefault(event.page_id, event)
            note_links = (await session.execute(select(SourceNoteMarkerLink).where(
                SourceNoteMarkerLink.revision_id == revision_id,
            ))).scalars().all()
            counts = dict((await session.execute(
                select(RevisionBlockCoverage.status, func.count(RevisionBlockCoverage.id))
                .where(RevisionBlockCoverage.revision_id == revision_id)
                .group_by(RevisionBlockCoverage.status)
            )).all())
            findings = structure_findings(
                nodes, span_rows, source_blocks=source_blocks,
                instrument_type=instrument_type, note_links=note_links,
            )
            findings.extend(source_page_review_findings(source_pages, latest_review))
            uncovered = counts.get("unmapped", 0) + counts.get("partial", 0)
            if uncovered:
                findings.append({"code": "source_blocks_need_review", "severity": "warning",
                                 "message": f"{uncovered} source blocks are partial or unmapped; inspect their full saved text.",
                                 "provision_id": None, "reference_path": None})
            return {"revision_id": revision_id, "provision_count": len(nodes),
                    "span_count": len(span_rows), "coverage_counts": counts,
                    "findings": findings}

    async def page_audit(self, revision_id: UUID, extraction_id: UUID, page_index: int) -> dict | None:
        """Show exactly which saved page blocks supplied which saved provisions."""
        async with self.db.session() as session:
            source = (await session.execute(select(RevisionSource).where(
                RevisionSource.revision_id == revision_id,
                RevisionSource.extraction_id == extraction_id,
            ))).scalar_one_or_none()
            if source is None:
                return None
            page = (await session.execute(select(FilePage).where(
                FilePage.extraction_id == source.extraction_id,
                FilePage.page_index == page_index,
            ))).scalar_one_or_none()
            if page is None:
                return None
            review = (await session.execute(select(ReviewEvent).where(
                ReviewEvent.file_extraction_id == source.extraction_id,
                ReviewEvent.page_id == page.id,
            ).order_by(ReviewEvent.created_at.desc(), ReviewEvent.id.desc()).limit(1))).scalar_one_or_none()
            blocks = (await session.execute(select(FileBlock).where(
                FileBlock.page_id == page.id).order_by(FileBlock.block_ordinal)
            )).scalars().all()
            coverage = {row.block_id: row for row in (await session.execute(
                select(RevisionBlockCoverage).where(
                    RevisionBlockCoverage.revision_id == revision_id,
                    RevisionBlockCoverage.page_id == page.id,
                )
            )).scalars().all()}
            spans_by_block: dict[UUID, list[dict]] = {}
            rows = (await session.execute(
                select(ProvisionSpan, Provision)
                .join(Provision, Provision.id == ProvisionSpan.provision_id)
                .where(ProvisionSpan.revision_id == revision_id,
                       ProvisionSpan.page_id == page.id)
                .order_by(Provision.ordinal, ProvisionSpan.provision_char_start)
            )).all()
            for span, node in rows:
                spans_by_block.setdefault(span.block_id, []).append({
                    "span_id": span.id, "provision_id": node.id,
                    "reference_path": node.reference_path, "kind": node.kind,
                    "label": node.label, "heading": node.heading,
                    "source_status": node.source_status,
                    "own_text": node.own_text, "mapping_quality": span.mapping_quality,
                    "provision_char_start": span.provision_char_start,
                    "provision_char_end": span.provision_char_end,
                    "block_char_start": span.block_char_start,
                    "block_char_end": span.block_char_end,
                })
            note_rows = (await session.execute(
                select(SourceNoteMarkerLink)
                .where(SourceNoteMarkerLink.revision_id == revision_id,
                       SourceNoteMarkerLink.page_id == page.id)
                .order_by(SourceNoteMarkerLink.block_char_start)
            )).scalars().all()
            linked_ids = {linked_id for link in note_rows
                          for linked_id in (link.note_provision_id, link.target_provision_id)
                          if linked_id is not None}
            linked_nodes = {item.id: item for item in (await session.execute(
                select(Provision).where(Provision.revision_id == revision_id,
                                        Provision.id.in_(linked_ids))
            )).scalars().all()} if linked_ids else {}
            blocks_by_id = {block.id: block for block in blocks}
            note_links_by_block: dict[UUID, list[dict]] = {}
            for link in note_rows:
                note = linked_nodes[link.note_provision_id]
                target = linked_nodes[link.target_provision_id] if link.target_provision_id else None
                block = blocks_by_id[link.block_id]
                note_links_by_block.setdefault(link.block_id, []).append({
                    "id": link.id, "marker_label": link.marker_label,
                    "marker_text": block.normalized_text[link.block_char_start:link.block_char_end],
                    "block_char_start": link.block_char_start,
                    "block_char_end": link.block_char_end,
                    "note_provision_id": note.id,
                    "note_reference_path": note.reference_path,
                    "note_text": note.own_text,
                    "target_provision_id": target.id if target else None,
                    "target_reference_path": target.reference_path if target else None,
                    "target_kind": target.kind if target else None,
                    "target_label": target.label if target else None,
                })
            return {
                "file_id": source.file_id, "extraction_id": source.extraction_id,
                "page_id": page.id, "page_index": page.page_index,
                "outcome": page.outcome, "extraction_method": page.extraction_method,
                "ocr_confidence": float(page.ocr_confidence) if page.ocr_confidence is not None else None,
                "raw_text": page.raw_text, "normalized_text": page.normalized_text,
                "text_sha256": page.text_sha256, "warnings": page.warnings,
                "table_regions": page.table_regions or [],
                "review_decision": review.decision if review else None,
                "review_reason": review.reason if review else None,
                "blocks": [{
                    "id": block.id, "ordinal": block.block_ordinal,
                    "kind": block.block_kind, "raw_text": block.raw_text,
                    "normalized_text": block.normalized_text,
                    "page_char_start": block.page_char_start,
                    "page_char_end": block.page_char_end,
                    "bbox": block.bbox, "warnings": block.warnings,
                    "coverage_status": coverage[block.id].status if block.id in coverage else None,
                    "coverage_reason": coverage[block.id].reason if block.id in coverage else None,
                    "spans": spans_by_block.get(block.id, []),
                    "note_links": note_links_by_block.get(block.id, []),
                } for block in blocks],
            }
