"""Read-only checks shared by the audit CLI and publication validation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from sqlalchemy import select, text

from app.db.legal_corpus_models import ChunkSet
from app.embeddings.encoder import build_embedding_input, get_tokenizer, input_sha256
from app.embeddings.models import EmbeddingProfileModel
from app.embeddings.profiles import ACTIVE_PROFILE, assert_profile_matches
from app.services.legal_chunking import ChunkConfig
from app.db.streaming import fetch_rows


async def audit_embeddings(session, *, chunk_set_ids=None, check_tokens=False) -> dict:
    profile = ACTIVE_PROFILE
    stored = await session.scalar(select(EmbeddingProfileModel).where(
        EmbeddingProfileModel.name == profile.name))
    issues = Counter()
    samples: dict[str, list[str]] = {}
    def flag(code, identity):
        issues[code] += 1
        samples.setdefault(code, [])
        if len(samples[code]) < 10:
            samples[code].append(str(identity))
    if stored is not None:
        try:
            assert_profile_matches(stored, profile)
        except ValueError:
            flag("profile_configuration_mismatch", stored.id)
    else:
        flag("missing_profile", profile.name)
    config = ChunkConfig()
    if chunk_set_ids is None:
        chunk_set_ids = list((await session.scalars(select(ChunkSet.id).where(
            ChunkSet.chunker_version == config.chunker_version,
            ChunkSet.config_sha256 == config.config_sha256))).all())
    rows = await fetch_rows(session, text("""
        SELECT c.id, c.chunk_set_id, c.revision_id, c.chunk_text, c.chunk_text_sha256,
               c.char_count, c.label, c.heading, i.canonical_title,
               r.review_state, r.content_sha256 AS revision_hash, cs.content_sha256 AS set_hash,
               e.input_sha256, e.created_at AS embedding_created_at,
               vector_dims(e.embedding) AS dims, vector_norm(e.embedding) AS norm,
               md5(e.embedding::text) AS vector_checksum,
               c.search_vector = to_tsvector('english'::regconfig, c.search_text) AS fts_valid
        FROM legal_chunks c JOIN chunk_sets cs ON cs.id=c.chunk_set_id
        JOIN corpus_revisions r ON r.id=c.revision_id
        JOIN legal_versions v ON v.id=r.legal_version_id
        JOIN legal_instruments i ON i.id=v.instrument_id
        LEFT JOIN legal_chunk_embeddings e ON e.chunk_id=c.id AND e.profile_id=:pid
        WHERE c.chunk_set_id=ANY(CAST(:ids AS uuid[])) ORDER BY c.id
    """), {"ids": chunk_set_ids, "pid": stored.id if stored else None}, mappings=True)
    inputs, fingerprint_rows = [], []
    for row in rows:
        identity = row["id"]
        if row["review_state"] != "approved" or row["revision_hash"] != row["set_hash"]:
            flag("unapproved_or_stale_chunk_set", identity)
        if input_sha256(row["chunk_text"]) != row["chunk_text_sha256"]:
            flag("chunk_hash_mismatch", identity)
        if len(row["chunk_text"]) != row["char_count"]:
            flag("chunk_char_count_mismatch", identity)
        if not row["fts_valid"]:
            flag("fts_vector_mismatch", identity)
        try:
            assembled = build_embedding_input(row["canonical_title"], row["label"],
                row["heading"], row["chunk_text"], profile)
            inputs.append((identity, assembled))
        except ValueError:
            flag("legacy_character_truncation", identity)
            assembled = None
        if row["input_sha256"] is None:
            flag("missing_embedding", identity)
        else:
            if assembled is not None and input_sha256(assembled) != row["input_sha256"]:
                flag("stale_embedding", identity)
            if row["dims"] != profile.dimension:
                flag("wrong_vector_dimension", identity)
            if row["norm"] is None or not .999 <= row["norm"] <= 1.001:
                flag("invalid_vector_norm", identity)
        fingerprint_rows.append((str(identity), row["input_sha256"], row["vector_checksum"],
                                 str(row["embedding_created_at"])))
    max_tokens = None
    if check_tokens:
        tokenizer = get_tokenizer(profile)
        lengths = [len(tokenizer.encode(t, add_special_tokens=True, truncation=False))
                   for _, t in inputs]
        max_tokens = max(lengths, default=0)
        for (identity, _), count in zip(inputs, lengths, strict=True):
            if count > profile.max_tokens:
                flag("token_limit_exceeded", identity)
    indexes = (await session.execute(text("""
        SELECT c.relname AS name, i.indisvalid AS valid, i.indisready AS ready,
               pg_get_indexdef(c.oid) AS definition
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relname IN ('ix_lce_embedding_hnsw','ix_legal_chunks_search_vector')
          AND n.nspname=current_schema()
    """))).mappings().all()
    by_name = {r["name"]: dict(r) for r in indexes}
    vector_version = await session.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    if not vector_version or tuple(int(v) for v in vector_version.split('.')[:2]) < (0, 8):
        flag("pgvector_requires_0_8_or_later", vector_version)
    for name, method in (("ix_lce_embedding_hnsw", "USING hnsw"),
                         ("ix_legal_chunks_search_vector", "USING gin")):
        item = by_name.get(name)
        if not item or not item["valid"] or not item["ready"] or method not in item["definition"]:
            flag("missing_or_invalid_index", name)
        elif method.endswith("hnsw") and "vector_cosine_ops" not in item["definition"]:
            flag("wrong_vector_index_operator", name)
    orphan_count = await session.scalar(text("""
        SELECT count(*) FROM legal_chunk_embeddings e LEFT JOIN legal_chunks c
        ON c.id=e.chunk_id AND c.revision_id=e.revision_id WHERE c.id IS NULL
    """))
    if orphan_count:
        issues["orphan_embeddings"] = orphan_count
    inventory = (await session.execute(text("""
        SELECT cs.chunker_version, count(DISTINCT cs.id) AS chunk_sets,
          count(c.id) AS chunks, count(e.chunk_id) AS embeddings_for_active_profile
        FROM chunk_sets cs LEFT JOIN legal_chunks c ON c.chunk_set_id=cs.id
        LEFT JOIN legal_chunk_embeddings e ON e.chunk_id=c.id AND e.profile_id=:pid
        GROUP BY cs.chunker_version ORDER BY cs.chunker_version
    """), {"pid": stored.id if stored else None})).mappings().all()
    return {
        "profile": profile.name, "profile_id": str(stored.id) if stored else None,
        "model_id": profile.model_id, "dimension": profile.dimension,
        "max_tokens": profile.max_tokens, "input_recipe": profile.input_recipe,
        "normalize": profile.normalize, "chunk_sets": len(chunk_set_ids), "chunks": len(rows),
        "pgvector_version": vector_version,
        "embedded_chunks": sum(r["input_sha256"] is not None for r in rows),
        "max_input_characters": max((len(t) for _, t in inputs), default=0),
        "token_check_performed": check_tokens, "maximum_observed_tokens": max_tokens,
        "issues": dict(issues), "samples": samples, "indexes": by_name,
        "inventory": [dict(r) for r in inventory],
        "vector_fingerprint": hashlib.sha256(json.dumps(fingerprint_rows).encode()).hexdigest(),
        "ok": bool(rows) and not issues,
        "provenance_limit": "Historical profile records model ID, not the original weights commit; "
                            "stored vectors cannot prove which model artifact originally produced them.",
    }
