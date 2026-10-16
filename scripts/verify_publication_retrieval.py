"""Opt-in PostgreSQL integration checks in a disposable, empty isolated schema.

Uses .env for the connection, but never changes the public corpus. Creates and
drops ONLY a newly named legalmind_verify_<random UUID> schema. No model download.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.legal_corpus_models import (
    AppUser, CorpusRevision, FileBlock, FileExtraction, FilePage, LegalFile,
    LegalFileAssignment, LegalInstrument, LegalVersion, Jurisdiction, Provision,
    ProvisionSpan, RevisionSource,
)
from app.db.session import Database
from app.embeddings.encoder import build_embedding_input, input_sha256
from app.embeddings.profiles import ACTIVE_PROFILE
from app.embeddings.repository import EmbeddingRepository
from app.repositories.legal_chunk_repository import LegalChunkRepository
from app.repositories.legal_search_repository import LegalSearchRepository, SCOPE
from app.repositories.publication_repository import PublicationRepository
from app.schemas.legal_search import LegalSearchIn
from app.services.corpus_validation import PublicationValidationError
from app.services.legal_retrieval import LegalRetrievalService
from app.services.legal_chunking import ChunkConfig

TABLES = ("app_users", "jurisdictions", "legal_instruments", "legal_versions", "legal_files",
    "legal_file_assignments", "file_extractions", "file_pages", "file_blocks", "corpus_revisions",
    "revision_sources", "provisions", "provision_spans", "chunk_sets", "legal_chunks",
    "legal_chunk_provisions", "embedding_profiles", "legal_chunk_embeddings")


def migration_sql():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261016_17_corpus_publications.py"
    spec = importlib.util.spec_from_file_location("stage910_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        module.upgrade()
    return output.getvalue()


async def fixture(db, title, *, with_suffix=False):
    now, actor, jurisdiction, law, version, file_id, extraction, page, revision = (
        datetime.now(UTC), *[uuid4() for _ in range(8)])
    bodies = [("section", "19", "Receipt", "19. A trader shall issue a receipt showing the date of sale and price.", None)]
    if with_suffix:
        bodies.append(("subsection", "2", None, "(2) A receipt shall show the seller name and address.", "19"))
        bodies.append(("section", "19A", "Returns", "19A. Return policies shall be displayed before purchase.", None))
    async with db.session() as s, s.begin():
        s.add(AppUser(id=actor, auth_provider="test", auth_subject=str(actor), role="admin"))
        s.add(Jurisdiction(id=jurisdiction, code=str(jurisdiction), name="Fixture"))
        s.add(LegalInstrument(id=law, jurisdiction_id=jurisdiction, instrument_type="act",
            canonical_title=title, citation_key=str(law), aliases=[]))
        s.add(LegalVersion(id=version, instrument_id=law, edition_key="fixture"))
        s.add(LegalFile(id=file_id, original_filename="fixture.pdf", object_key=str(file_id),
            sha256=input_sha256(str(file_id)), byte_size=100, media_type="application/pdf", page_count=1))
        s.add(LegalFileAssignment(file_id=file_id, legal_version_id=version, assignment_role="primary"))
        s.add(FileExtraction(id=extraction, file_id=file_id, engine_name="fixture", engine_version="1",
            config_sha256="a"*64, output_sha256="b"*64, processing_status="completed", completed_at=now))
        s.add(FilePage(id=page, extraction_id=extraction, page_index=1, extraction_method="native", outcome="extracted"))
        s.add(CorpusRevision(id=revision, legal_version_id=version, revision_number=1,
            parser_version="fixture", normalizer_version="fixture", content_sha256="c"*64,
            review_state="approved", approved_by=actor, approved_at=now))
        s.add(RevisionSource(revision_id=revision, legal_version_id=version, extraction_id=extraction,
                            file_id=file_id, source_role="primary"))
        await s.flush()
        provision_ids = {}
        for ordinal, (kind, label, heading, body, parent) in enumerate(bodies, 1):
            block_id, provision_id = uuid4(), uuid4()
            provision_ids[label] = provision_id
            # Column sets follow the existing persisted corpus schema.
            block_columns = FileBlock.__table__.columns
            block_data = {"id": block_id, "page_id": page, "extraction_id": extraction,
                "block_ordinal": ordinal, "block_kind": "paragraph", "raw_text": body,
                "normalized_text": body, "text_sha256": input_sha256(body)}
            s.add(FileBlock(**{k: v for k, v in block_data.items() if k in block_columns}))
            path = f"f:{file_id}/s:{parent}/sub:{label}" if parent else f"f:{file_id}/s:{label}"
            s.add(Provision(id=provision_id, revision_id=revision, kind=kind, label=label,
                parent_id=provision_ids[parent] if parent else None,
                reference_path=path, ordinal=ordinal, heading=heading,
                own_text=body, text_sha256=input_sha256(body)))
            await s.flush()
            s.add(ProvisionSpan(id=uuid4(), provision_id=provision_id, revision_id=revision,
                extraction_id=extraction, page_id=page, block_id=block_id,
                provision_char_start=0, provision_char_end=len(body), block_char_start=0,
                block_char_end=len(body), mapping_quality="exact"))
    build = await LegalChunkRepository(db).build_for_revision(revision)
    repo = EmbeddingRepository(db)
    profile_id = await repo.ensure_profile(ACTIVE_PROFILE)
    work = await repo.chunks_without_embeddings(profile_id, revision_id=revision)
    await repo.upsert_embeddings(profile_id, [{"chunk_id": row["chunk_id"], "revision_id": revision,
        "input_sha256": input_sha256(build_embedding_input(row["canonical_title"], row["label"],
            row["heading"], row["chunk_text"], ACTIVE_PROFILE)),
        "embedding": [1.0, 0.0] + [0.0]*1022 if row["label"] == "19" else [0.0, 1.0] + [0.0]*1022}
        for row in work])
    return {"law": law, "revision": revision, "chunk_set": build["chunk_set_id"], "actor": actor, "approved_at": now}


async def run(output=None):
    schema = "legalmind_verify_" + uuid4().hex
    assert re.fullmatch(r"legalmind_verify_[0-9a-f]{32}", schema)
    settings = get_settings().model_copy(update={"db_pool_size": 2, "db_max_overflow": 0})
    admin, db = Database(settings), Database(settings)
    @event.listens_for(db.engine.sync_engine, "connect")
    def set_schema(dbapi, _):
        dbapi.run_async(lambda c: c.execute(f'SET search_path TO "{schema}", public, extensions'))
    checks = []
    created = False
    def passed(name):
        checks.append(name)
        print(json.dumps({"passed": name}), flush=True)
    try:
        async with admin.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute(f'CREATE SCHEMA "{schema}"')
            created = True
            script = f'SET LOCAL search_path TO "{schema}", public, extensions;\n'
            script += "\n".join(f'CREATE TABLE "{schema}"."{t}" (LIKE public."{t}" INCLUDING ALL);' for t in TABLES)
            script += "\nCREATE INDEX ix_legal_chunks_search_vector ON legal_chunks USING gin(search_vector);"
            script += "\nCREATE INDEX ix_lce_embedding_hnsw ON legal_chunk_embeddings USING hnsw(embedding vector_cosine_ops);"
            script += "\n" + migration_sql()
            await raw.driver_connection.execute(script)
        passed("additive migration executes on real PostgreSQL")
        first = await fixture(db, "Fixture Consumer Protection Act", with_suffix=True)
        hidden = await fixture(db, "Fixture Unpublished Act")
        embedding_repo = EmbeddingRepository(db)
        profile_id = await embedding_repo.get_profile_id(ACTIVE_PROFILE)
        async with db.session() as s, s.begin():
            await s.execute(text("UPDATE legal_chunk_embeddings SET input_sha256=:hash WHERE revision_id=:id"),
                {"hash": "0"*64, "id": hidden["revision"]})
        stale = await embedding_repo.chunks_without_embeddings(profile_id)
        assert len(stale) == 1 and stale[0]["state"] == "stale" and stale[0]["revision_id"] == hidden["revision"]
        def replacement(row):
            return {"chunk_id": row["chunk_id"], "revision_id": row["revision_id"],
                    "input_sha256": row["expected_input_sha256"], "embedding": [1.0]+[0.0]*1023}
        assert await embedding_repo.upsert_embeddings(profile_id, [replacement(stale[0])]) == 1
        assert await embedding_repo.upsert_embeddings(profile_id, [replacement(stale[0])]) == 0
        passed("stale vector repair touches only the stale row and leaves valid rows unchanged")
        async with db.session() as s, s.begin():
            await s.execute(text("DELETE FROM legal_chunk_embeddings WHERE revision_id=:id"), {"id": hidden["revision"]})
        try:
            await PublicationRepository(db).validate([hidden["chunk_set"]])
            raise AssertionError("Publication accepted missing vectors")
        except PublicationValidationError: pass
        missing = await embedding_repo.chunks_without_embeddings(profile_id)
        assert len(missing) == 1 and missing[0]["state"] == "missing"
        await embedding_repo.upsert_embeddings(profile_id, [replacement(missing[0])])
        passed("missing vectors block publication and can be repaired independently")
        async with db.session() as s, s.begin():
            await s.execute(text("UPDATE legal_chunk_embeddings SET embedding=(embedding + embedding) WHERE revision_id=:id"), {"id": hidden["revision"]})
        invalid = await embedding_repo.chunks_without_embeddings(profile_id)
        assert len(invalid) == 1 and invalid[0]["state"] == "invalid"
        await embedding_repo.upsert_embeddings(profile_id, [replacement(invalid[0])])
        passed("invalid normalization is detected even when the input hash still matches")
        pub = PublicationRepository(db)
        a = await pub.create("fixture-a", [first["chunk_set"]])
        again = await pub.create("fixture-a", [first["chunk_set"]])
        assert not again["created"] and a["publication_id"] == again["publication_id"]
        passed("publication creation is idempotent")
        await pub.activate(a["publication_id"])
        alternate = await LegalChunkRepository(db).build_for_revision(first["revision"], config=ChunkConfig(max_chars=2000))
        assert alternate["created"] and alternate["chunk_set_id"] != first["chunk_set"]
        passed("publication sealing still permits new independent chunk-set builds")
        async def encode(*args, **kwargs): return [[1.0]+[0.0]*1023]
        service = LegalRetrievalService(LegalSearchRepository(db), settings, encode=encode)
        exact = await service.search(LegalSearchIn(query="Section 19", instrument_id=first["law"]))
        assert exact.status == "ok" and {r.label for r in exact.results} == {"19"}
        assert exact.results[0].source_refs[0]["page_index"] == 1
        passed("exact 19 does not match 19A and retains source provenance")
        nested = await service.search(LegalSearchIn(query="Section 19(2)", instrument_id=first["law"]))
        assert nested.status == "ok" and all(path.endswith("/s:19/sub:2")
            for r in nested.results for path in r.matched_reference_paths)
        passed("subsection lookup follows the stored hierarchy and source fragments")
        missing = await service.search(LegalSearchIn(query="Section 999", instrument_id=first["law"]))
        assert missing.status == "no_results"
        passed("nonexistent exact reference returns no unrelated fallback")
        hidden_query = await service.search(LegalSearchIn(query="receipt", instrument_id=hidden["law"]))
        assert hidden_query.status == "no_results"
        hybrid = await service.search(LegalSearchIn(query="trader receipt sale price"))
        assert hybrid.status == "ok" and all(r.instrument_id == first["law"] for r in hybrid.results)
        assert hybrid.results[0].keyword_rank and hybrid.results[0].vector_rank and hybrid.results[0].rrf_rank
        passed("FTS plus vector RRF excludes an equally similar unpublished instrument")
        # Constraints are tested in rollback-only savepoints, never by leaving
        # the fixture or the public database damaged.
        for name, statement, params in [
            ("sealed membership cannot be deleted", "DELETE FROM publication_items WHERE publication_id=:id", {"id": a["publication_id"]}),
            ("published vector cannot be overwritten", "UPDATE legal_chunk_embeddings SET input_sha256=:hash WHERE revision_id=:id", {"id": first["revision"], "hash": "f"*64}),
            ("published source text cannot be changed", "UPDATE provisions SET own_text='changed' WHERE revision_id=:id", {"id": first["revision"]}),
            ("profile identity cannot be changed under existing vectors", "UPDATE embedding_profiles SET max_tokens=512", {}),
        ]:
            rejected = False
            async with db.session() as s, s.begin():
                try:
                    async with s.begin_nested(): await s.execute(text(statement), params)
                except DBAPIError: rejected = True
            assert rejected, name
            passed(name)
        b = await pub.create("fixture-b", [first["chunk_set"], hidden["chunk_set"]])
        try:
            await pub.activate(b["publication_id"], expected_manifest_sha256="0"*64)
            raise AssertionError("Bad manifest accepted")
        except PublicationValidationError: pass
        active, _ = await LegalSearchRepository(db).active_catalog()
        assert active["id"] == a["publication_id"]
        passed("failed activation leaves previous publication active")
        await pub.activate(b["publication_id"])
        ambiguous = await service.search(LegalSearchIn(query="Section 19"))
        assert ambiguous.status == "needs_clarification" and not ambiguous.results
        passed("cross-law exact-reference ambiguity is explicit")
        # Explicitly revoke fixture approval, then restore it after proving
        # fail-closed behavior. The public corpus is never involved.
        async with db.session() as s, s.begin():
            await s.execute(text("UPDATE corpus_revisions SET review_state='rejected',approved_by=NULL,approved_at=NULL WHERE id=:id"), {"id": hidden["revision"]})
        unavailable = await service.search(LegalSearchIn(query="receipt"))
        assert unavailable.status == "no_active_publication" and not unavailable.results
        passed("approval revocation invalidates the complete active release")
        try:
            await pub.activate(b["publication_id"])
            raise AssertionError("Revoked revision activated")
        except PublicationValidationError: pass
        passed("activation revalidates approval even for an already active publication")
        async with db.session() as s, s.begin():
            await s.execute(text("UPDATE corpus_revisions SET review_state='approved',approved_by=:actor,approved_at=:at WHERE id=:id"),
                {"id": hidden["revision"], "actor": hidden["actor"], "at": hidden["approved_at"]})
        concurrent = await asyncio.gather(pub.activate(a["publication_id"]),
            pub.activate(b["publication_id"]), return_exceptions=True)
        assert any(isinstance(result, dict) for result in concurrent)
        assert all(isinstance(result, (dict, PublicationValidationError)) for result in concurrent), concurrent
        assert sum(p["status"] == "active" for p in await pub.list()) == 1
        passed("concurrent activations commit safely or report busy and leave exactly one active publication")
        async with db.session() as s, s.begin():
            await s.execute(text("SET LOCAL enable_seqscan=off"))
            plan = (await s.execute(text("EXPLAIN SELECT e.chunk_id FROM legal_chunk_embeddings e ORDER BY e.embedding <=> (SELECT embedding FROM legal_chunk_embeddings LIMIT 1) LIMIT 5"))).scalars().all()
            assert any("Index Scan" in line and ("hnsw" in line or "embedding" in line) for line in plan)
        passed("PostgreSQL can execute the existing cosine HNSW index")
        result = {"passed": len(checks), "checks": checks, "hnsw_plan": plan,
                  "isolated_schema": schema, "public_corpus_modified": False}
        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return result
    finally:
        await db.dispose()
        if created:
            # schema is generated above and never comes from user input.
            assert re.fullmatch(r"legalmind_verify_[0-9a-f]{32}", schema)
            async with admin.engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True,
        help="Explicitly run disposable-schema integration checks on the configured PostgreSQL")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.output))
