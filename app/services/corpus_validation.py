"""Reproduce chunk inputs from approved source spans before sealing a release.

Rebuilding here is a pure comparison: it writes no chunks and encodes no vectors.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import defer

from app.db.legal_corpus_models import (
    ChunkSet, CorpusRevision, FileBlock, FilePage, LegalChunk, LegalChunkProvision,
    LegalInstrument, LegalVersion, Provision, ProvisionSpan,
)
from app.services.embedding_audit import audit_embeddings
from app.db.streaming import fetch_rows
from app.services.legal_chunking import (
    ChunkConfig, ProvisionNode, ProvisionSourceSpan, build_chunks_from_spans,
)

logger = logging.getLogger(__name__)


class PublicationValidationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("Publication validation failed: " + "; ".join(issues[:8]))


async def validate_corpus(session, chunk_set_ids: list[UUID], *, check_tokens=True) -> dict:
    if not chunk_set_ids or len(set(chunk_set_ids)) != len(chunk_set_ids):
        raise PublicationValidationError(["Select a nonempty list of distinct chunk-set IDs"])
    config = ChunkConfig()
    logger.info("Validating %d selected chunk sets", len(chunk_set_ids))
    selected = (await session.execute(select(ChunkSet, CorpusRevision, LegalVersion, LegalInstrument)
        .join(CorpusRevision, CorpusRevision.id == ChunkSet.revision_id)
        .join(LegalVersion, LegalVersion.id == CorpusRevision.legal_version_id)
        .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
        .where(ChunkSet.id.in_(chunk_set_ids)).order_by(ChunkSet.id))).all()
    errors = []
    if len(selected) != len(chunk_set_ids):
        errors.append("One or more selected chunk sets do not exist")
    instrument_ids = [i.id for _, _, _, i in selected]
    if len(set(instrument_ids)) != len(instrument_ids):
        errors.append("Select exactly one revision and edition per legal instrument")
    revision_ids = [r.id for _, r, _, _ in selected]
    nodes_by_revision = defaultdict(list)
    for p in await fetch_rows(session, select(Provision).where(
            Provision.revision_id.in_(revision_ids)).order_by(Provision.ordinal), scalars=True):
        nodes_by_revision[p.revision_id].append(ProvisionNode(
            id=p.id, parent_id=p.parent_id, kind=p.kind, label=p.label,
            reference_path=p.reference_path, ordinal=p.ordinal, heading=p.heading,
            own_text=p.own_text, source_status=p.source_status))
    exact_text_matches = (func.substr(Provision.own_text, ProvisionSpan.provision_char_start + 1,
        ProvisionSpan.provision_char_end - ProvisionSpan.provision_char_start) ==
        func.substr(FileBlock.normalized_text, ProvisionSpan.block_char_start + 1,
                    ProvisionSpan.block_char_end - ProvisionSpan.block_char_start))
    block_hash = func.encode(func.sha256(func.convert_to(FileBlock.normalized_text, "UTF8")), "hex")
    span_rows = await fetch_rows(session, select(ProvisionSpan, FilePage.page_index,
        FileBlock.block_ordinal, block_hash, exact_text_matches)
        .select_from(ProvisionSpan)
        .join(Provision, Provision.id == ProvisionSpan.provision_id)
        .join(FilePage, FilePage.id == ProvisionSpan.page_id)
        .outerjoin(FileBlock, FileBlock.id == ProvisionSpan.block_id)
        .where(ProvisionSpan.revision_id.in_(revision_ids)))
    spans_by_revision = defaultdict(list)
    source_fingerprints = defaultdict(list)
    node_map = {p.id: p for nodes in nodes_by_revision.values() for p in nodes}
    for span, page_index, block_ordinal, source_block_hash, exact_match in span_rows:
        if span.provision_id not in node_map:        # a span must belong to a loaded provision
            raise KeyError(span.provision_id)
        if span.mapping_quality == "exact" and not exact_match:
            errors.append(f"Source span {span.id} differs from the saved PDF block")
        spans_by_revision[span.revision_id].append(ProvisionSourceSpan(
            id=span.id, provision_id=span.provision_id, extraction_id=span.extraction_id,
            page_index=page_index, block_ordinal=block_ordinal or 0,
            block_char_start=span.block_char_start or 0,
            provision_char_start=span.provision_char_start,
            provision_char_end=span.provision_char_end, mapping_quality=span.mapping_quality))
        source_fingerprints[span.revision_id].append((str(span.id), str(span.page_id),
            str(span.block_id), page_index, block_ordinal, span.block_char_start,
            span.block_char_end, source_block_hash))
    chunks_by_set = defaultdict(list)
    chunks = await fetch_rows(session, select(LegalChunk).options(defer(LegalChunk.search_vector)).where(
        LegalChunk.chunk_set_id.in_(chunk_set_ids)).order_by(LegalChunk.ordinal), scalars=True)
    for c in chunks:
        chunks_by_set[c.chunk_set_id].append(c)
    links_by_chunk = defaultdict(list)
    for link in await fetch_rows(session, select(LegalChunkProvision).where(
            LegalChunkProvision.chunk_id.in_([c.id for c in chunks]))
            .order_by(LegalChunkProvision.ordinal), scalars=True):
        links_by_chunk[link.chunk_id].append(link)
    sources = (await session.execute(text("""
        SELECT rs.revision_id, rs.extraction_id, fe.processing_status,
          lf.id AS file_id, lf.sha256, lf.page_count, lf.source_url, lf.publisher,
          count(fp.id) AS saved_pages,
          count(fp.id) FILTER (WHERE fp.outcome='failed') AS failed_pages
        FROM revision_sources rs JOIN file_extractions fe ON fe.id=rs.extraction_id
        JOIN legal_files lf ON lf.id=rs.file_id
        LEFT JOIN file_pages fp ON fp.extraction_id=fe.id
        WHERE rs.revision_id=ANY(CAST(:ids AS uuid[])) GROUP BY rs.revision_id,
          rs.extraction_id,fe.processing_status,lf.id
    """), {"ids": revision_ids})).mappings().all()
    source_revisions = {r["revision_id"] for r in sources}
    logger.info("Loaded %d chunks and %d source spans for publication validation", len(chunks), len(span_rows))
    for row in sources:
        if row["processing_status"] != "completed" or row["saved_pages"] != row["page_count"] or row["failed_pages"]:
            errors.append(f"Incomplete source extraction {row['extraction_id']}")
    items, chunk_fingerprints = [], []
    fields = ("root_provision_id", "ordinal", "kind", "label", "heading", "reference_path",
              "chunk_key", "sequence_in_root", "root_total_chunks", "prefix", "chunk_text",
              "char_count", "chunk_text_sha256", "search_text")
    segment_fields = ("source_span_id", "provision_id", "provision_char_start",
                      "provision_char_end", "chunk_char_start", "chunk_char_end")
    for cs, rev, version, instrument in selected:
        if rev.review_state != "approved" or not rev.approved_by or not rev.approved_at:
            errors.append(f"Revision {rev.id} has no recorded approval")
        if cs.content_sha256 != rev.content_sha256:
            errors.append(f"Stale chunk-set revision hash: {cs.id}")
        if cs.chunker_version != config.chunker_version or cs.config_sha256 != config.config_sha256:
            errors.append(f"Unsupported or historical chunk configuration: {cs.id}")
            continue
        if rev.id not in source_revisions:
            errors.append(f"Revision {rev.id} has no source extraction")
        try:
            expected = build_chunks_from_spans(instrument.canonical_title,
                nodes_by_revision[rev.id], spans_by_revision[rev.id], config)
        except ValueError as exc:
            errors.append(f"Revision {rev.id}: {exc}")
            continue
        actual = chunks_by_set[cs.id]
        if not actual or len(actual) != len(expected.chunks) or len(actual) != cs.chunk_report.get("chunk_count"):
            errors.append(f"Chunk set {cs.id} is incomplete")
        expected_map = {c.chunk_key: c for c in expected.chunks}
        for c in actual:
            draft = expected_map.get(c.chunk_key)
            if draft is None or any(getattr(c, f) != getattr(draft, f) for f in fields):
                errors.append(f"Chunk {c.id} differs from its approved source reconstruction")
                continue
            expected_segments = [tuple(getattr(s, f) for f in segment_fields) for s in draft.segments]
            actual_segments = [tuple(getattr(s, f) for f in segment_fields) for s in links_by_chunk[c.id]]
            if actual_segments != expected_segments:
                errors.append(f"Chunk {c.id} has incomplete or changed source fragments")
            chunk_fingerprints.append((str(c.id), c.chunk_text_sha256,
                hashlib.sha256(c.search_text.encode()).hexdigest(),
                [[str(v) if isinstance(v, UUID) else v for v in s] for s in actual_segments]))
        items.append({"chunk_set_id": str(cs.id), "revision_id": str(rev.id),
            "legal_version_id": str(version.id), "instrument_id": str(instrument.id),
            "instrument_title": instrument.canonical_title, "edition_key": version.edition_key,
            "applicability_status": version.applicability_status,
            "content_sha256": rev.content_sha256, "chunk_count": len(actual),
            "approved_by": str(rev.approved_by) if rev.approved_by else None,
            "approved_at": rev.approved_at.isoformat() if rev.approved_at else None})
    embeddings = await audit_embeddings(session, chunk_set_ids=chunk_set_ids, check_tokens=check_tokens)
    logger.info("Publication source reconstruction and embedding checks complete")
    if not embeddings["ok"]:
        errors.extend(f"Embedding audit: {key} ({value})" for key, value in embeddings["issues"].items())
    if errors:
        raise PublicationValidationError(errors)
    fingerprint = {"items": items, "chunks": sorted(chunk_fingerprints),
        "source_spans": {str(k): sorted(v) for k, v in source_fingerprints.items()},
        "sources": [dict(r) for r in sorted(sources, key=lambda r: str(r["extraction_id"]))],
        "profile": embeddings["profile"], "vector_fingerprint": embeddings["vector_fingerprint"]}
    manifest_hash = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str,
        separators=(",", ":")).encode()).hexdigest()
    return {"items": items, "manifest_sha256": manifest_hash,
        "chunk_count": len(chunks), "embedding_audit": embeddings,
        "review_scope": "Technical publication of explicitly approved revisions; existing local "
                        "approvals and unresolved review exceptions are not a legal accuracy certification.",
        "validation_version": "publication-v1-source-reproduction"}
