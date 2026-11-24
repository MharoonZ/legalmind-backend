"""Read the active publication from PostgreSQL into a ``PublicationSnapshot``.

Reads are plain SELECTs over the existing schema (no migration is required).
A publication is immutable and identified by its manifest hash, so a loaded
snapshot is also written to a local cache; later starts read the cache after
one small identity query instead of transferring the corpus again.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from uuid import UUID

import numpy as np
from sqlalchemy import text

from app.embeddings.profiles import EmbeddingProfile
from app.retrieval.snapshot import (
    Chunk, Instrument, ProvisionNode, PublicationSnapshot, Segment, SnapshotError,
)

logger = logging.getLogger(__name__)

_CACHE_FORMAT = 2

_ACTIVE = text("""
    SELECT p.id, p.name, p.manifest_sha256, p.chunk_count,
           ep.id AS profile_id, ep.name AS profile_name, ep.model_id, ep.dimension,
           ep.max_tokens, ep.input_recipe, ep.normalize
    FROM corpus_publications p
    JOIN embedding_profiles ep ON ep.id = p.profile_id
    WHERE p.status = 'active'
""")

_ITEMS = text("""
    SELECT pi.instrument_id, pi.legal_version_id, pi.revision_id, pi.chunk_set_id,
           pi.content_sha256, i.canonical_title, i.citation_key, i.aliases, i.instrument_type,
           j.code AS jurisdiction_code, v.edition_key, v.applicability_status,
           rev.review_state, rev.content_sha256 AS revision_hash,
           rev.legal_version_id AS revision_version, cs.content_sha256 AS set_hash,
           cs.revision_id AS set_revision
    FROM publication_items pi
    JOIN corpus_revisions rev ON rev.id = pi.revision_id
    JOIN chunk_sets cs ON cs.id = pi.chunk_set_id
    JOIN legal_versions v ON v.id = pi.legal_version_id
    JOIN legal_instruments i ON i.id = pi.instrument_id
    JOIN jurisdictions j ON j.id = i.jurisdiction_id
    WHERE pi.publication_id = :publication_id
    ORDER BY i.canonical_title, pi.instrument_id
""")

_CHUNKS = text("""
    SELECT c.id, pi.instrument_id, c.root_provision_id, c.ordinal, c.kind, c.label, c.heading,
           c.reference_path, c.sequence_in_root, c.root_total_chunks, c.prefix, c.chunk_text,
           e.embedding::real[] AS embedding
    FROM legal_chunks c
    JOIN publication_items pi ON pi.chunk_set_id = c.chunk_set_id AND pi.revision_id = c.revision_id
    LEFT JOIN legal_chunk_embeddings e ON e.chunk_id = c.id AND e.revision_id = c.revision_id
        AND e.profile_id = :profile_id
    WHERE pi.publication_id = :publication_id
    ORDER BY pi.instrument_id, c.ordinal, c.id
""")

_NODES = text("""
    SELECT p.id, pi.instrument_id, p.parent_id, p.kind, p.label, p.heading, p.reference_path,
           p.ordinal, p.own_text, p.source_status
    FROM provisions p
    JOIN publication_items pi ON pi.revision_id = p.revision_id
    WHERE pi.publication_id = :publication_id
    ORDER BY pi.instrument_id, p.ordinal
""")

_SEGMENTS = text("""
    SELECT l.chunk_id, l.chunk_char_start, l.chunk_char_end, l.provision_char_start,
           l.provision_char_end, l.provision_id, l.source_span_id,
           p.kind, p.label, p.reference_path, p.text_sha256 AS provision_text_sha256,
           s.mapping_quality, s.extraction_id, s.page_id, s.block_id,
           fp.page_index, fb.text_sha256 AS block_text_sha256,
           lf.id AS file_id, lf.sha256 AS file_sha256, lf.original_filename, lf.source_url,
           lf.publisher
    FROM legal_chunk_provisions l
    JOIN legal_chunks c ON c.id = l.chunk_id AND c.revision_id = l.revision_id
    JOIN publication_items pi ON pi.chunk_set_id = c.chunk_set_id AND pi.revision_id = c.revision_id
    JOIN provisions p ON p.id = l.provision_id
    JOIN provision_spans s ON s.id = l.source_span_id
    JOIN file_pages fp ON fp.id = s.page_id
    LEFT JOIN file_blocks fb ON fb.id = s.block_id
    JOIN file_extractions fe ON fe.id = s.extraction_id
    JOIN legal_files lf ON lf.id = fe.file_id
    WHERE pi.publication_id = :publication_id
    ORDER BY l.chunk_id, l.ordinal
""")


async def active_identity(db) -> tuple[UUID, str] | None:
    """The active publication's identity; a single small read."""
    async with db.session() as session:
        row = (await session.execute(_ACTIVE)).mappings().first()
    return (row["id"], row["manifest_sha256"]) if row else None


async def load_snapshot(db, *, cache_dir: str | None = None) -> PublicationSnapshot | None:
    """Load the active publication, preferring a verified local cache."""
    started = perf_counter()
    async with db.session() as session:
        publication = (await session.execute(_ACTIVE)).mappings().first()
        if publication is None:
            return None
        cached = _read_cache(cache_dir, publication["id"], publication["manifest_sha256"])
        if cached is not None:
            logger.info("publication_snapshot source=cache publication=%s chunks=%d elapsed_ms=%.0f",
                        cached.id, len(cached.chunks), (perf_counter() - started) * 1000)
            return cached
        params = {"publication_id": publication["id"], "profile_id": publication["profile_id"]}
        items = (await session.execute(_ITEMS, params)).mappings().all()
        chunk_rows = (await session.execute(_CHUNKS, params)).mappings().all()
        node_rows = (await session.execute(_NODES, params)).mappings().all()
        segment_rows = (await session.execute(_SEGMENTS, params)).mappings().all()
    snapshot = _assemble(publication, items, chunk_rows, node_rows, segment_rows)
    _write_cache(cache_dir, snapshot)
    logger.info("publication_snapshot source=database publication=%s laws=%d chunks=%d nodes=%d elapsed_ms=%.0f",
                snapshot.id, len(snapshot.instruments), len(snapshot.chunks), len(snapshot.nodes),
                (perf_counter() - started) * 1000)
    return snapshot


def _assemble(publication, items, chunk_rows, node_rows, segment_rows) -> PublicationSnapshot:
    if not items:
        raise SnapshotError("The active publication has no pinned sources.")
    instruments = []
    for item in items:
        if (item["review_state"] != "approved" or item["revision_hash"] != item["content_sha256"]
                or item["set_hash"] != item["content_sha256"]
                or item["set_revision"] != item["revision_id"]
                or item["revision_version"] != item["legal_version_id"]):
            raise SnapshotError("A published source no longer matches its approved revision.")
        aliases = item["aliases"] if isinstance(item["aliases"], list) else []
        instruments.append(Instrument(
            id=item["instrument_id"], title=item["canonical_title"], citation_key=item["citation_key"],
            aliases=tuple(alias for alias in aliases if isinstance(alias, str)),
            instrument_type=item["instrument_type"], jurisdiction_code=item["jurisdiction_code"],
            legal_version_id=item["legal_version_id"], edition_key=item["edition_key"],
            applicability_status=item["applicability_status"], revision_id=item["revision_id"],
            chunk_set_id=item["chunk_set_id"]))
    if len(chunk_rows) != publication["chunk_count"]:
        raise SnapshotError("The active publication is incomplete: its chunk count changed.")
    segments: dict[UUID, list[Segment]] = defaultdict(list)
    for row in segment_rows:
        if row["mapping_quality"] != "exact" or row["chunk_char_start"] is None:
            continue
        segments[row["chunk_id"]].append(Segment(
            start=row["chunk_char_start"], end=row["chunk_char_end"],
            provision_id=row["provision_id"], kind=row["kind"], label=row["label"],
            reference_path=row["reference_path"], source_span_id=row["source_span_id"],
            file_id=row["file_id"], file_sha256=row["file_sha256"],
            filename=row["original_filename"], source_url=row["source_url"],
            page_index=row["page_index"],
            # Plain JSON values, so a cached snapshot equals a freshly loaded one.
            extras={"extraction_id": str(row["extraction_id"]), "publisher": row["publisher"],
                    "mapping_quality": row["mapping_quality"], "page_id": str(row["page_id"]),
                    "block_id": str(row["block_id"]) if row["block_id"] else None,
                    "block_text_sha256": row["block_text_sha256"],
                    "provision_text_sha256": row["provision_text_sha256"],
                    "provision_char_start": row["provision_char_start"],
                    "provision_char_end": row["provision_char_end"]}))
    dimension = publication["dimension"]
    vectors = np.zeros((len(chunk_rows), dimension), dtype=np.float32)
    chunks = []
    for index, row in enumerate(chunk_rows):
        embedding = row["embedding"]
        if embedding is None or len(embedding) != dimension:
            raise SnapshotError("A published chunk has no vector for the publication's embedding profile.")
        vectors[index] = embedding
        chunks.append(Chunk(
            index=index, id=row["id"], instrument_id=row["instrument_id"],
            root_provision_id=row["root_provision_id"], ordinal=row["ordinal"], kind=row["kind"],
            label=row["label"], heading=row["heading"], reference_path=row["reference_path"],
            sequence_in_root=row["sequence_in_root"], root_total_chunks=row["root_total_chunks"],
            prefix=row["prefix"], text=row["chunk_text"],
            segments=tuple(sorted(segments.get(row["id"], ()), key=lambda s: (s.start, s.end)))))
    nodes = [ProvisionNode(
        id=row["id"], instrument_id=row["instrument_id"], parent_id=row["parent_id"],
        kind=row["kind"], label=row["label"], heading=row["heading"],
        reference_path=row["reference_path"], ordinal=row["ordinal"], text=row["own_text"],
        source_status=row["source_status"]) for row in node_rows]
    profile = EmbeddingProfile(
        name=publication["profile_name"], model_id=publication["model_id"], dimension=dimension,
        max_tokens=publication["max_tokens"], input_recipe=publication["input_recipe"],
        normalize=publication["normalize"])
    return PublicationSnapshot(
        publication_id=publication["id"], name=publication["name"],
        manifest_sha256=publication["manifest_sha256"], profile=profile,
        instruments=instruments, chunks=chunks, vectors=vectors, nodes=nodes)


# ------------------------------------------------------------------ cache
def _cache_paths(cache_dir: str | None, publication_id, manifest: str) -> tuple[Path, Path] | None:
    if not cache_dir:
        return None
    stem = Path(cache_dir) / f"{publication_id}-{manifest[:16]}-v{_CACHE_FORMAT}"
    return stem.with_suffix(".json.gz"), stem.with_suffix(".npy")


def _plain(value):
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Cannot cache {type(value).__name__}")


def _write_cache(cache_dir: str | None, snapshot: PublicationSnapshot) -> None:
    paths = _cache_paths(cache_dir, snapshot.id, snapshot.manifest_sha256)
    if paths is None:
        return
    meta_path, vector_path = paths
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": _CACHE_FORMAT, "id": snapshot.id, "name": snapshot.name,
            "manifest_sha256": snapshot.manifest_sha256, "profile": asdict(snapshot.profile),
            "instruments": [asdict(item) for item in snapshot.instruments.values()],
            "chunks": [asdict(chunk) for chunk in snapshot.chunks],
            "nodes": [asdict(node) for node in snapshot.nodes.values()],
        }
        for target, writer in ((meta_path, lambda handle: handle.write(gzip.compress(
                json.dumps(payload, default=_plain, ensure_ascii=False).encode("utf-8")))),
                               (vector_path, lambda handle: np.save(handle, snapshot.vectors, allow_pickle=False))):
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                writer(handle)
                temporary = handle.name
            os.replace(temporary, target)
    except OSError as exc:      # a cache is an optimisation; never fail a request for it
        logger.warning("publication_cache_write_failed error=%s", type(exc).__name__)


def _read_cache(cache_dir: str | None, publication_id, manifest: str) -> PublicationSnapshot | None:
    paths = _cache_paths(cache_dir, publication_id, manifest)
    if paths is None or not all(path.is_file() for path in paths):
        return None
    meta_path, vector_path = paths
    try:
        payload = json.loads(gzip.decompress(meta_path.read_bytes()).decode("utf-8"))
        if (payload.get("format") != _CACHE_FORMAT or payload["id"] != str(publication_id)
                or payload["manifest_sha256"] != manifest):
            return None
        vectors = np.load(vector_path, allow_pickle=False)

        def identities(row, *names):
            return {**row, **{name: UUID(row[name]) for name in names if row.get(name) is not None}}

        instruments = [Instrument(**{**identities(row, "id", "legal_version_id", "revision_id", "chunk_set_id"),
                                     "aliases": tuple(row["aliases"])}) for row in payload["instruments"]]
        chunks = []
        for row in payload["chunks"]:
            parts = tuple(Segment(**identities(part, "provision_id", "source_span_id", "file_id"))
                          for part in row["segments"])
            chunks.append(Chunk(**{**identities(row, "id", "instrument_id", "root_provision_id"),
                                   "segments": parts}))
        nodes = [ProvisionNode(**identities(row, "id", "instrument_id", "parent_id")) for row in payload["nodes"]]
        return PublicationSnapshot(
            publication_id=UUID(payload["id"]), name=payload["name"],
            manifest_sha256=payload["manifest_sha256"], profile=EmbeddingProfile(**payload["profile"]),
            instruments=instruments, chunks=chunks, vectors=vectors, nodes=nodes)
    except (OSError, ValueError, KeyError, TypeError, SnapshotError) as exc:
        logger.warning("publication_cache_unreadable error=%s", type(exc).__name__)
        return None
