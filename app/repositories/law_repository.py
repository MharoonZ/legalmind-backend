"""Read-only database access for the legal corpus.

Every query joins `law_sources` and filters on `review_status`, because a
passage may only be quoted to a user while its source is in an allowed review
state. Nothing in this module writes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RetrievalError

logger = logging.getLogger(__name__)

# Columns every retrieval query returns, so one row parser handles them all.
_SELECT_COLUMNS = """
    p.evidence_id            AS evidence_id,
    p.page_number            AS page_number,
    p.text                   AS text,
    s.id                     AS section_id,
    s.section_number         AS section_number,
    s.title                  AS section_title,
    src.id                   AS source_id,
    src.law_name             AS law_name,
    src.jurisdiction         AS jurisdiction,
    src.source_url           AS source_url,
    src.review_status        AS review_status
"""

_JOINS = """
    FROM law_passages p
    JOIN law_sections s ON s.id = p.section_id
    JOIN law_sources src ON src.id = s.source_id
"""

# asyncpg cannot encode a Python string into the `vector` type directly, so we
# cast the parameter to text first and let PostgreSQL parse it.
_VECTOR_PARAM = "CAST(CAST(:query_embedding AS text) AS vector)"

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset(
    """
    a an and are as at be by can could do does for from has have how i if in is it its
    me my of on or please that the their them then there these this to was what when
    where which who why will with would you your
    """.split()
)


@dataclass(frozen=True, slots=True)
class PassageHit:
    """One retrieved passage plus everything needed to cite it."""

    evidence_id: str
    section_id: str
    section_number: int
    section_title: str
    page_number: int
    text: str
    law_name: str
    jurisdiction: str
    source_url: str
    source_id: str
    review_status: str
    score: float = 0.0
    retriever: str = "unknown"

    @property
    def citation_label(self) -> str:
        return f"{self.law_name} ({self.jurisdiction}), section {self.section_number}"


@dataclass(frozen=True, slots=True)
class CorpusStats:
    """Used by /health/retrieval to show whether the pipeline is ready."""

    sources: int
    sections: int
    passages: int
    embedded_passages: int
    embedding_models: tuple[str, ...]
    review_statuses: tuple[str, ...]


def _row_to_hit(row, retriever: str) -> PassageHit:
    mapping = row._mapping
    return PassageHit(
        evidence_id=mapping["evidence_id"],
        section_id=mapping["section_id"],
        section_number=mapping["section_number"],
        section_title=mapping["section_title"],
        page_number=mapping["page_number"],
        text=mapping["text"],
        law_name=mapping["law_name"],
        jurisdiction=mapping["jurisdiction"],
        source_url=mapping["source_url"],
        source_id=mapping["source_id"],
        review_status=mapping["review_status"],
        score=float(mapping.get("score") or 0.0),
        retriever=retriever,
    )


def keyword_terms(question: str) -> list[str]:
    """Content words used for the loose keyword fallback query."""
    words = [w.lower() for w in _WORD_RE.findall(question)]
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


class LawRepository:
    """All corpus reads used by POST /ask."""

    def __init__(self, session: AsyncSession, review_statuses: list[str]) -> None:
        self.session = session
        self.review_statuses = review_statuses

    # --------------------------------------------------------------- helpers
    async def _fetch(self, sql: str, params: dict, retriever: str) -> list[PassageHit]:
        statement = text(sql).bindparams(bindparam("review_statuses", expanding=True))
        try:
            result = await self.session.execute(
                statement, {**params, "review_statuses": self.review_statuses}
            )
        except SQLAlchemyError as exc:
            logger.exception("Corpus query failed (%s)", retriever)
            raise RetrievalError(f"{retriever} search failed") from exc
        return [_row_to_hit(row, retriever) for row in result]

    # ------------------------------------------------------ direct lookup
    async def fetch_sections(self, section_numbers: list[int]) -> list[PassageHit]:
        """Every passage of the named sections, in reading order.

        Used when the question already names a section ("what does section 10
        say"), which is a stronger signal than any similarity score.
        """
        if not section_numbers:
            return []
        sql = f"""
            SELECT {_SELECT_COLUMNS}, 1.0 AS score
            {_JOINS}
            WHERE s.section_number IN :section_numbers
              AND src.review_status IN :review_statuses
            ORDER BY s.section_number, p.page_number
        """
        statement = text(sql).bindparams(
            bindparam("section_numbers", expanding=True),
            bindparam("review_statuses", expanding=True),
        )
        try:
            result = await self.session.execute(
                statement,
                {
                    "section_numbers": section_numbers,
                    "review_statuses": self.review_statuses,
                },
            )
        except SQLAlchemyError as exc:
            logger.exception("Section lookup failed")
            raise RetrievalError("section lookup failed") from exc
        return [_row_to_hit(row, "section") for row in result]

    # ----------------------------------------------------- keyword search
    async def keyword_search(self, question: str, limit: int) -> list[PassageHit]:
        """Full-text search over the generated `search_vector` column (GIN)."""
        sql = f"""
            WITH q AS (SELECT websearch_to_tsquery('english', :question) AS tsq)
            SELECT {_SELECT_COLUMNS},
                   ts_rank_cd(p.search_vector, q.tsq) AS score
            {_JOINS}
            CROSS JOIN q
            WHERE p.search_vector @@ q.tsq
              AND src.review_status IN :review_statuses
            ORDER BY score DESC, p.evidence_id
            LIMIT :limit
        """
        hits = await self._fetch(sql, {"question": question, "limit": limit}, "keyword")
        if hits:
            return hits
        return await self._loose_keyword_search(question, limit)

    async def _loose_keyword_search(self, question: str, limit: int) -> list[PassageHit]:
        """OR-match on content words when the strict AND query finds nothing."""
        terms = keyword_terms(question)
        if not terms:
            return []
        tsquery = " | ".join(terms)
        sql = f"""
            WITH q AS (SELECT to_tsquery('english', :tsquery) AS tsq)
            SELECT {_SELECT_COLUMNS},
                   ts_rank_cd(p.search_vector, q.tsq) AS score
            {_JOINS}
            CROSS JOIN q
            WHERE p.search_vector @@ q.tsq
              AND src.review_status IN :review_statuses
            ORDER BY score DESC, p.evidence_id
            LIMIT :limit
        """
        return await self._fetch(sql, {"tsquery": tsquery, "limit": limit}, "keyword")

    # ------------------------------------------------------ vector search
    async def vector_search(
        self, embedding: list[float], embedding_model: str, limit: int
    ) -> list[PassageHit]:
        """Cosine nearest neighbours over `law_passages.embedding` (HNSW).

        Rows embedded with a different model are skipped: distances between
        two models' vectors are meaningless.
        """
        sql = f"""
            SELECT {_SELECT_COLUMNS},
                   1 - (p.embedding <=> {_VECTOR_PARAM}) AS score
            {_JOINS}
            WHERE p.embedding IS NOT NULL
              AND p.embedding_model = :embedding_model
              AND src.review_status IN :review_statuses
            ORDER BY p.embedding <=> {_VECTOR_PARAM}
            LIMIT :limit
        """
        params = {
            "query_embedding": "[" + ",".join(f"{v:.6f}" for v in embedding) + "]",
            "embedding_model": embedding_model,
            "limit": limit,
        }
        return await self._fetch(sql, params, "vector")

    # ------------------------------------------------------------- health
    async def stats(self) -> CorpusStats:
        sql = """
            SELECT
                (SELECT count(*) FROM law_sources)   AS sources,
                (SELECT count(*) FROM law_sections)  AS sections,
                (SELECT count(*) FROM law_passages)  AS passages,
                (SELECT count(*) FROM law_passages WHERE embedding IS NOT NULL)
                    AS embedded_passages,
                (SELECT coalesce(array_agg(DISTINCT embedding_model), '{}')
                   FROM law_passages WHERE embedding_model IS NOT NULL)
                    AS embedding_models,
                (SELECT coalesce(array_agg(DISTINCT review_status), '{}')
                   FROM law_sources) AS review_statuses
        """
        try:
            row = (await self.session.execute(text(sql))).one()
        except SQLAlchemyError as exc:
            logger.exception("Corpus stats query failed")
            raise RetrievalError("corpus stats unavailable") from exc
        mapping = row._mapping
        return CorpusStats(
            sources=mapping["sources"],
            sections=mapping["sections"],
            passages=mapping["passages"],
            embedded_passages=mapping["embedded_passages"],
            embedding_models=tuple(mapping["embedding_models"] or ()),
            review_statuses=tuple(mapping["review_statuses"] or ()),
        )
