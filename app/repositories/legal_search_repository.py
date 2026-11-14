"""Publication-scoped exact, PostgreSQL FTS, and HNSW vector candidate reads."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from time import perf_counter
from types import SimpleNamespace

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text

from app.embeddings.profiles import ACTIVE_PROFILE, assert_profile_matches
from app.repositories.legal_chunk_repository import LegalChunkRepository
from app.services.retrieval_fusion import filter_keyword_candidates, reciprocal_rank_fusion

logger = logging.getLogger(__name__)

# Every retrieval branch uses this same allow-list join. No client-supplied
# revision IDs, 'latest revision' heuristic, or unscoped vector result is used.
_PUBLICATION_SCOPE = """
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
"""
SCOPE = _PUBLICATION_SCOPE + """
   AND (CAST(:instrument_id AS uuid) IS NULL OR i.id=CAST(:instrument_id AS uuid))
   AND (CAST(:jurisdiction AS text) IS NULL OR upper(j.code)=upper(CAST(:jurisdiction AS text)))
"""
PROJECTION = """c.id AS chunk_id,c.chunk_set_id,c.revision_id,c.root_provision_id,
 c.kind,c.label,c.heading,c.reference_path,c.sequence_in_root,c.root_total_chunks,c.chunk_text,
 c.ordinal,i.id AS instrument_id,i.canonical_title AS instrument_title,i.citation_key,
 v.id AS legal_version_id,v.edition_key,v.applicability_status,j.code AS jurisdiction_code"""

_CATALOG_FIELDS = ("instrument_id", "canonical_title", "citation_key", "aliases", "jurisdiction_code", "edition_key")

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
            # One round trip for the publication, immutable embedding profile,
            # and scoped counts grouped by published instrument.
            rows = (await session.execute(text("""
                SELECT p.id, p.name AS publication_name, p.chunk_count,
                       (SELECT count(*) FROM publication_items WHERE publication_id=p.id) AS instrument_count,
                       ep.name, ep.model_id, ep.dimension, ep.max_tokens, ep.input_recipe, ep.normalize,
                       catalog.*
                FROM corpus_publications p
                JOIN embedding_profiles ep ON ep.id=p.profile_id
                LEFT JOIN LATERAL (
                    SELECT i.id AS instrument_id, i.canonical_title, i.citation_key, i.aliases,
                           j.code AS jurisdiction_code, v.edition_key, count(*) AS scoped_chunks
            """ + _PUBLICATION_SCOPE.replace(":publication_id", "p.id") + """
                    GROUP BY i.id, j.code, v.edition_key
                ) catalog ON true
                WHERE p.status='active'
                ORDER BY catalog.canonical_title
            """))).mappings().all()
            if not rows:
                return None, []
            publication = rows[0]
            assert_profile_matches(SimpleNamespace(**publication), ACTIVE_PROFILE)
            instruments = [{field: row[field] for field in _CATALOG_FIELDS}
                           for row in rows if row["instrument_id"] is not None]
            if (sum(row["scoped_chunks"] or 0 for row in rows) != publication["chunk_count"]
                    or len(instruments) != publication["instrument_count"]):
                return None, []
            return {"id": publication["id"], "name": publication["publication_name"]}, instruments

    async def retrieve(self, publication_id, *, instrument_id, jurisdiction, query, reference,
                       query_vector, pool_size, min_similarity, ef_search, result_limit=10, rrf_k=60,
                       alternative_keyword=None, instrument_ids=None):
        params = {"publication_id": publication_id, "instrument_id": instrument_id,
                  "jurisdiction": jurisdiction, "query": query, "pool": pool_size,
                  "alternative": alternative_keyword}
        scope = SCOPE
        if instrument_ids:
            params["instrument_ids"] = list(instrument_ids)
            scope += " AND i.id=ANY(CAST(:instrument_ids AS uuid[])) "
        async with self.snapshot() as session:
            complete = await session.scalar(text(
                "SELECT p.chunk_count = (SELECT count(*) " + _PUBLICATION_SCOPE + ") "
                "FROM corpus_publications p WHERE p.id=:publication_id AND p.status='active'"
            ), params)
            if not complete:
                raise PublicationChanged("The active publication changed or failed its completeness check")
            exact, keyword, vector = [], [], []
            if reference is not None:
                started = perf_counter()
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
                logger.info("legal_exact candidates=%d elapsed_ms=%.0f", len(exact),
                            (perf_counter() - started) * 1000)
            else:
                started = perf_counter()
                # Strict lexical matches lead; relaxed OR matches require at
                # least two surviving lexemes (one only for a one-term query).
                lexical = """WITH terms AS (
                    SELECT variant, raw, tsvector_to_array(to_tsvector('english',raw)) AS lexemes
                    FROM (VALUES (0,CAST(:query AS text)),(1,CAST(:alternative AS text))) AS inputs(variant,raw)
                    WHERE raw <> ''
                    ),
                    queries AS (SELECT variant, plainto_tsquery('english',raw) AS strict,
                        phraseto_tsquery('english',raw) AS phrase,
                        coalesce((SELECT string_agg(quote_literal(w),' | ')
                          FROM unnest(lexemes) AS w),'')::tsquery AS loose,
                        lexemes FROM terms) """
                matched_terms = """(SELECT count(*) FROM unnest(tsvector_to_array(c.search_vector)) AS lex
                    WHERE lex=ANY(q.lexemes))"""
                lexical_match = " AND c.search_vector @@ q.loose AND (" + matched_terms + \
                    " >= least(2,cardinality(q.lexemes))) "
                cosine = "1 - (e.embedding <=> :qv)" if query_vector is not None else "NULL::double precision"
                keyword_statement = text(lexical +
                    "SELECT hit.*, q.variant FROM queries q CROSS JOIN LATERAL (SELECT " + PROJECTION +
                    ", ts_rank_cd(c.search_vector,q.loose) AS keyword_score, "
                    "(c.search_vector @@ q.strict) AS keyword_strict_match, " +
                    "(cardinality(q.lexemes)>1 AND c.search_vector @@ q.phrase) AS keyword_phrase_match, " +
                    matched_terms + " AS keyword_matched_terms, cardinality(q.lexemes) AS keyword_total_terms, " +
                    cosine + " AS lexical_vector_score " + scope + lexical_match +
                    " ORDER BY keyword_phrase_match DESC,keyword_strict_match DESC,keyword_matched_terms DESC, "
                    "keyword_score DESC,i.canonical_title,c.ordinal,c.id LIMIT :pool) hit "
                    "ORDER BY hit.keyword_phrase_match DESC,hit.keyword_strict_match DESC,hit.keyword_matched_terms DESC,"
                    "hit.keyword_score DESC,q.variant,hit.instrument_title,hit.ordinal,hit.chunk_id")
                keyword_params = params
                if query_vector is not None:
                    keyword_statement = keyword_statement.bindparams(bindparam("qv", type_=Vector(1024)))
                    keyword_params = {**params, "qv": query_vector}
                keyword = [dict(r) for r in (await session.execute(keyword_statement, keyword_params)).mappings().all()]
                accepted = filter_keyword_candidates(keyword, semantic_available=query_vector is not None,
                                                     min_similarity=min_similarity)
                # Try standalone lexical coverage when dense agreement would
                # otherwise discard the entire keyword channel.
                if not accepted:
                    accepted = filter_keyword_candidates(keyword, semantic_available=False,
                                                         min_similarity=min_similarity)
                unique = {}
                for row in accepted:
                    unique.setdefault(row["chunk_id"], row)
                keyword = list(unique.values())[:pool_size]
                logger.info("legal_keyword matched=%d retained=%d elapsed_ms=%.0f",
                            len(accepted), len(keyword), (perf_counter() - started) * 1000)
                if query_vector is not None:
                    started = perf_counter()
                    # pgvector >=0.8 filtered HNSW searches must scan beyond
                    # rejected (unpublished/other-profile) neighbors.
                    await session.execute(text("SELECT set_config('hnsw.ef_search',:ef,true), "
                                               "set_config('hnsw.iterative_scan','strict_order',true)"),
                                          {"ef": str(ef_search)})
                    statement = text("SELECT " + PROJECTION +
                        ", 1 - (e.embedding <=> :qv) AS vector_score " + scope +
                        " ORDER BY e.embedding <=> :qv LIMIT :pool").bindparams(bindparam("qv", type_=Vector(1024)))
                    nearest = (await session.execute(statement, {**params, "qv": query_vector})).mappings().all()
                    vector = [dict(r) for r in nearest if r["vector_score"] >= min_similarity]
                    vector.sort(key=lambda r: (-r["vector_score"], r["instrument_title"],
                                               r["ordinal"], str(r["chunk_id"])))
                    logger.info("legal_vector nearest=%d retained=%d elapsed_ms=%.0f",
                                len(nearest), len(vector), (perf_counter() - started) * 1000)
            # Read fragment/source payload only for the final candidates,
            # while still in the same publication snapshot.
            if reference is not None:
                paths = {(str(r["instrument_id"]), path) for r in exact
                         for path in r["matched_reference_paths"]}
                selected_ids = [r["chunk_id"] for r in exact[:result_limit]] if len(paths) == 1 else []
            else:
                selected_ids = [r["chunk_id"] for r in
                    reciprocal_rank_fusion(keyword, vector, k=rrf_k)[:result_limit]]
            sources = await LegalChunkRepository.source_refs_for_ids(session, selected_ids)
            for rows in (exact, keyword, vector):
                for row in rows:
                    row["source_refs"] = sources.get(row["chunk_id"], [])
            return exact, keyword, vector
