"""Publication-scoped exact, PostgreSQL FTS, and HNSW vector candidate reads."""
from __future__ import annotations

from contextlib import asynccontextmanager

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import load_only

from app.db.legal_corpus_models import LegalChunk
from app.db.publication_models import CorpusPublication
from app.embeddings.models import EmbeddingProfileModel
from app.embeddings.profiles import ACTIVE_PROFILE, assert_profile_matches
from app.repositories.legal_chunk_repository import LegalChunkRepository
from app.services.retrieval_fusion import filter_keyword_candidates, reciprocal_rank_fusion

# Every retrieval branch uses this same allow-list join. No client-supplied
# revision IDs, 'latest revision' heuristic, or unscoped vector result is used.
SCOPE = """
 FROM legal_chunks c
 JOIN publication_items pi ON pi.chunk_set_id=c.chunk_set_id AND pi.revision_id=c.revision_id
 JOIN corpus_publications pub ON pub.id=pi.publication_id AND pub.status='active'
 JOIN chunk_sets cs ON cs.id=c.chunk_set_id AND cs.content_sha256=pi.content_sha256
 JOIN corpus_revisions rev ON rev.id=pi.revision_id AND rev.review_state='approved'
   AND rev.content_sha256=pi.content_sha256
 JOIN legal_versions v ON v.id=pi.legal_version_id
 JOIN legal_instruments i ON i.id=pi.instrument_id
 JOIN jurisdictions j ON j.id=i.jurisdiction_id
 JOIN legal_chunk_embeddings e ON e.chunk_id=c.id AND e.revision_id=c.revision_id
   AND e.profile_id=pub.profile_id
 WHERE pub.id=:publication_id
   AND (CAST(:instrument_id AS uuid) IS NULL OR i.id=CAST(:instrument_id AS uuid))
   AND (CAST(:jurisdiction AS text) IS NULL OR upper(j.code)=upper(CAST(:jurisdiction AS text)))
"""
PROJECTION = """c.id AS chunk_id,c.chunk_set_id,c.revision_id,c.root_provision_id,
 c.kind,c.label,c.heading,c.reference_path,c.sequence_in_root,c.root_total_chunks,c.chunk_text,
 c.ordinal,i.id AS instrument_id,i.canonical_title AS instrument_title,i.citation_key,
 v.id AS legal_version_id,v.edition_key,v.applicability_status,j.code AS jurisdiction_code"""


class PublicationChanged(RuntimeError):
    pass


class LegalSearchRepository:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def snapshot(self):
        async with self.db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            yield session

    async def active_catalog(self):
        async with self.snapshot() as session:
            publication = await session.scalar(select(CorpusPublication).where(CorpusPublication.status == "active"))
            if publication is None:
                return None, []
            profile = await session.get(EmbeddingProfileModel, publication.profile_id)
            assert_profile_matches(profile, ACTIVE_PROFILE)
            params = {"publication_id": publication.id, "instrument_id": None, "jurisdiction": None}
            count = await session.scalar(text("SELECT count(*) " + SCOPE), params)
            if count != publication.chunk_count:
                # Revocation or damaged membership invalidates the complete
                # release. Do not silently search a partial corpus.
                return None, []
            instruments = (await session.execute(text("""SELECT DISTINCT i.id AS instrument_id,
                i.canonical_title,i.citation_key,i.aliases,j.code AS jurisdiction_code,
                v.edition_key """ + SCOPE + " ORDER BY i.canonical_title"), params)).mappings().all()
            return {"id": publication.id, "name": publication.name}, [dict(r) for r in instruments]

    async def retrieve(self, publication_id, *, instrument_id, jurisdiction, query, reference,
                       query_vector, pool_size, min_similarity, ef_search, result_limit=10, rrf_k=60):
        params = {"publication_id": publication_id, "instrument_id": instrument_id,
                  "jurisdiction": jurisdiction, "query": query, "pool": pool_size}
        async with self.snapshot() as session:
            current = await session.scalar(select(CorpusPublication).where(CorpusPublication.status == "active"))
            if current is None or current.id != publication_id:
                raise PublicationChanged("The active publication changed while the query was prepared")
            count = await session.scalar(text("SELECT count(*) " + SCOPE),
                {**params, "instrument_id": None, "jurisdiction": None})
            if count != current.chunk_count:
                raise PublicationChanged("The active publication failed its completeness check")
            exact, keyword, vector = [], [], []
            if reference is not None:
                from app.services.legal_query import reference_parts
                root, children = reference_parts(reference)
                params.update(kind=reference.kind, root=root, children=children,
                              path=reference.reference_path)
                refs = """WITH RECURSIVE refs AS (
                    SELECT p.id,p.reference_path,p.id AS root_id,0 AS depth
                    FROM provisions p WHERE p.kind=:kind AND lower(p.label)=lower(:root)
                    UNION ALL
                    SELECT p.id,p.reference_path,t.root_id,t.depth+1 FROM provisions p
                    JOIN refs t ON p.parent_id=t.id
                    WHERE t.depth<cardinality(CAST(:children AS text[]))
                      AND lower(p.label)=lower((CAST(:children AS text[]))[t.depth+1])
                ) """
                matches = """ AND EXISTS (SELECT 1 FROM refs t WHERE t.root_id=c.root_provision_id
                    AND t.depth=cardinality(CAST(:children AS text[]))
                    AND (CAST(:path AS text) IS NULL OR t.reference_path=CAST(:path AS text))
                    AND (t.depth=0 OR EXISTS (SELECT 1 FROM legal_chunk_provisions l
                      JOIN provisions lp ON lp.id=l.provision_id WHERE l.chunk_id=c.id
                      AND (lp.reference_path=t.reference_path OR
                           left(lp.reference_path,length(t.reference_path)+1)=t.reference_path||'/')))) """
                target_paths = """ARRAY(SELECT t.reference_path FROM refs t
                    WHERE t.root_id=c.root_provision_id AND t.depth=cardinality(CAST(:children AS text[]))
                    AND (CAST(:path AS text) IS NULL OR t.reference_path=CAST(:path AS text)))"""
                exact = [dict(r) for r in (await session.execute(text(refs + "SELECT " + PROJECTION +
                    ", " + target_paths + " AS matched_reference_paths " + SCOPE + matches +
                    " ORDER BY i.canonical_title,c.ordinal,c.id LIMIT :pool"), params)).mappings().all()]
            else:
                # Strict lexical matches lead; relaxed OR matches require at
                # least two surviving lexemes (one only for a one-term query).
                lexical = """WITH terms AS (SELECT tsvector_to_array(to_tsvector('english',:query)) AS lexemes),
                    q AS (SELECT websearch_to_tsquery('english',:query) AS strict,
                        coalesce((SELECT string_agg(quote_literal(w),' | ')
                          FROM unnest(lexemes) AS w),'')::tsquery AS loose,
                        lexemes FROM terms) """
                lexical_match = """ AND c.search_vector @@ (SELECT loose FROM q)
                    AND (c.search_vector @@ (SELECT strict FROM q) OR
                      (SELECT count(*) FROM unnest(tsvector_to_array(c.search_vector)) AS lex
                       WHERE lex=ANY((SELECT lexemes FROM q)::text[])) >=
                       least(2,cardinality((SELECT lexemes FROM q)))) """
                matched_terms = """(SELECT count(*) FROM unnest(tsvector_to_array(c.search_vector)) AS lex
                    WHERE lex=ANY((SELECT lexemes FROM q)::text[]))"""
                cosine = "1 - (e.embedding <=> :qv)" if query_vector is not None else "NULL::double precision"
                keyword_statement = text(lexical + "SELECT " + PROJECTION +
                    ", ts_rank_cd(c.search_vector,(SELECT loose FROM q)) AS keyword_score, "
                    "(c.search_vector @@ (SELECT strict FROM q)) AS keyword_strict_match, " +
                    matched_terms + " AS keyword_matched_terms, cardinality((SELECT lexemes FROM q)) AS keyword_total_terms, " +
                    cosine + " AS lexical_vector_score " + SCOPE + lexical_match +
                    " ORDER BY keyword_strict_match DESC,keyword_matched_terms DESC, "
                    "keyword_score DESC,i.canonical_title,c.ordinal,c.id LIMIT :pool")
                keyword_params = params
                if query_vector is not None:
                    keyword_statement = keyword_statement.bindparams(bindparam("qv", type_=Vector(1024)))
                    keyword_params = {**params, "qv": query_vector}
                keyword = [dict(r) for r in (await session.execute(keyword_statement, keyword_params)).mappings().all()]
                keyword = filter_keyword_candidates(keyword, semantic_available=query_vector is not None,
                    min_similarity=min_similarity)
                if query_vector is not None:
                    # pgvector >=0.8 filtered HNSW searches must scan beyond
                    # rejected (unpublished/other-profile) neighbors.
                    await session.execute(text("SELECT set_config('hnsw.ef_search',:ef,true)"), {"ef": str(ef_search)})
                    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
                    statement = text("SELECT " + PROJECTION +
                        ", 1 - (e.embedding <=> :qv) AS vector_score " + SCOPE +
                        " ORDER BY e.embedding <=> :qv LIMIT :pool").bindparams(bindparam("qv", type_=Vector(1024)))
                    nearest = (await session.execute(statement, {**params, "qv": query_vector})).mappings().all()
                    vector = [dict(r) for r in nearest if r["vector_score"] >= min_similarity]
                    vector.sort(key=lambda r: (-r["vector_score"], r["instrument_title"],
                                               r["ordinal"], str(r["chunk_id"])))
            # Read fragment/source payload only for the final candidates,
            # while still in the same publication snapshot.
            if reference is not None:
                paths = {(str(r["instrument_id"]), path) for r in exact
                         for path in r["matched_reference_paths"]}
                selected_ids = [r["chunk_id"] for r in exact[:result_limit]] if len(paths) == 1 else []
            else:
                selected_ids = [r["chunk_id"] for r in
                    reciprocal_rank_fusion(keyword, vector, k=rrf_k)[:result_limit]]
            chunks = (await session.scalars(select(LegalChunk).options(load_only(LegalChunk.id))
                .where(LegalChunk.id.in_(selected_ids)))).all() if selected_ids else []
            sources = await LegalChunkRepository._source_refs(session, chunks)
            for rows in (exact, keyword, vector):
                for row in rows:
                    row["source_refs"] = sources.get(row["chunk_id"], [])
            return exact, keyword, vector
