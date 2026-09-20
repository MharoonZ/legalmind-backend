"""The POST /ask pipeline.

    question
      -> parse (does it name a section?)
      -> direct section fetch  |  keyword search + vector search
      -> merge with RRF, keep top K
      -> evidence gate: too weak -> "unsupported", no LLM call
      -> build grounded prompt -> OpenAI -> JSON
      -> validate citations against retrieved passages
      -> AskResponse

The gate before the LLM matters as much as the LLM: an unanswerable question
must cost nothing and must never reach a model that might guess.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import EmbeddingError, LLMError
from app.repositories.law_repository import LawRepository, PassageHit
from app.schemas.ask import AskMeta, AskResponse, Citation
from app.services.citations import ValidatedCitation, validate_citations
from app.services.embeddings import EmbeddingService
from app.services.fusion import FusedHit, reciprocal_rank_fusion
from app.services.llm.base import LLMClient
from app.services.prompt import SYSTEM_PROMPT, build_user_prompt
from app.services.question_parser import parse_question

logger = logging.getLogger(__name__)

NO_EVIDENCE_MESSAGE = (
    "I can only answer questions that are supported by sections 1 to 30 of the "
    "Contract Act, 1872. I could not find support for this question in those sections."
)
OUT_OF_RANGE_MESSAGE = (
    "This assistant currently covers only sections 1 to 30 of the Contract Act, "
    "1872, so I cannot answer about section {sections}."
)
MODEL_REFUSED_MESSAGE = (
    "The retrieved sections of the Contract Act, 1872 do not clearly answer this "
    "question, so I will not guess."
)


@dataclass(slots=True)
class _Retrieval:
    """Everything the gate and the prompt need from the search stage."""

    hits: list[FusedHit]
    strategy: str
    retrievers: list[str]
    top_keyword_score: float | None
    top_vector_score: float | None


class AskService:
    def __init__(
        self,
        repository: LawRepository,
        embeddings: EmbeddingService,
        llm: LLMClient,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.embeddings = embeddings
        self.llm = llm
        self.settings = settings

    # ------------------------------------------------------------- entrypoint
    async def ask(self, question: str) -> AskResponse:
        parsed = parse_question(
            question,
            section_min=self.settings.corpus_section_min,
            section_max=self.settings.corpus_section_max,
        )

        # A question that names only sections we do not carry must be refused
        # up front. Falling through to search would match the *word* "section"
        # in some unrelated passage and cite the wrong law back at the user.
        if parsed.out_of_range_sections and not parsed.section_numbers:
            sections = ", ".join(str(n) for n in parsed.out_of_range_sections)
            return AskResponse.unsupported(
                OUT_OF_RANGE_MESSAGE.format(sections=sections), None
            )

        retrieval = await self._retrieve(parsed.text, list(parsed.section_numbers))
        meta = self._build_meta(retrieval)

        if not retrieval.hits:
            return AskResponse.unsupported(NO_EVIDENCE_MESSAGE, meta)

        if not self._evidence_is_strong_enough(retrieval):
            logger.info("Evidence gate rejected question: %.80s", parsed.text)
            return AskResponse.unsupported(NO_EVIDENCE_MESSAGE, meta)

        answer = await self._generate_answer(parsed.text, retrieval.hits)
        retrieved = {hit.evidence_id: hit.passage for hit in retrieval.hits}

        if not answer.supported:
            return AskResponse.unsupported(answer.answer or MODEL_REFUSED_MESSAGE, meta)

        validated = validate_citations(answer.citations, retrieved)
        if not validated:
            # The model claimed support but cited nothing we retrieved. That is
            # exactly the hallucination this endpoint exists to prevent.
            logger.warning("Dropping supported answer with no valid citation")
            return AskResponse.unsupported(NO_EVIDENCE_MESSAGE, meta)

        return self._build_response(answer.answer, validated, meta)

    # -------------------------------------------------------------- retrieval
    async def _retrieve(self, question: str, section_numbers: list[int]) -> _Retrieval:
        settings = self.settings

        if section_numbers:
            direct = await self.repository.fetch_sections(section_numbers)
            if direct:
                # A named section is an exact structural match; similarity
                # search cannot improve on it.
                hits = [
                    FusedHit(
                        passage=passage,
                        rrf_score=1.0,
                        retrievers=["section"],
                        ranks={"section": rank},
                        scores={"section": 1.0},
                    )
                    for rank, passage in enumerate(direct, start=1)
                ][: max(settings.retrieval_top_k, len(section_numbers))]
                return _Retrieval(
                    hits=hits,
                    strategy="direct_section",
                    retrievers=["section"],
                    top_keyword_score=None,
                    top_vector_score=None,
                )

        # One AsyncSession is not safe for concurrent queries, so the two
        # searches run one after the other. Encoding the question happens in a
        # worker thread and touches no connection, so that part can overlap
        # with the keyword query.
        embed_task = asyncio.create_task(self._embed_question(question))
        try:
            keyword_hits = await self.repository.keyword_search(
                question, settings.retrieval_keyword_limit
            )
        finally:
            query_vector = await embed_task

        vector_hits: list[PassageHit] = []
        if query_vector is not None:
            vector_hits = await self.repository.vector_search(
                query_vector,
                self.embeddings.model_name,
                settings.retrieval_vector_limit,
            )

        retrievers = ["keyword"]
        if vector_hits:
            retrievers.append("vector")

        fused = reciprocal_rank_fusion(
            [keyword_hits, vector_hits],
            k=settings.retrieval_rrf_k,
            top_k=settings.retrieval_top_k,
        )
        return _Retrieval(
            hits=fused,
            strategy="hybrid" if vector_hits else "keyword_only",
            retrievers=retrievers,
            top_keyword_score=_best_score(keyword_hits),
            top_vector_score=_best_score(vector_hits),
        )

    async def _embed_question(self, question: str) -> list[float] | None:
        """Encode the question, or return None so search degrades to keywords."""
        if not self.settings.embeddings_enabled:
            return None
        try:
            return await self.embeddings.embed_query(question)
        except EmbeddingError as exc:
            logger.warning("Vector search skipped: %s", exc)
            return None

    # ----------------------------------------------------------- evidence gate
    def _evidence_is_strong_enough(self, retrieval: _Retrieval) -> bool:
        if retrieval.strategy == "direct_section":
            return True
        keyword = retrieval.top_keyword_score or 0.0
        vector = retrieval.top_vector_score or 0.0
        return (
            keyword >= self.settings.retrieval_min_keyword_rank
            or vector >= self.settings.retrieval_min_vector_similarity
        )

    # ---------------------------------------------------------------- answering
    async def _generate_answer(self, question: str, hits: list[FusedHit]):
        if not self.llm.configured:
            raise LLMError(
                "No LLM is configured. Set OPENAI_API_KEY in .env and restart the API."
            )
        prompt = build_user_prompt(question, hits)
        logger.debug("Sending %d passages to %s", len(hits), self.llm.name)
        return await self.llm.generate(SYSTEM_PROMPT, prompt)

    def _build_response(
        self, answer: str, validated: list[ValidatedCitation], meta: AskMeta | None
    ) -> AskResponse:
        citations = [
            Citation(
                evidence_id=item.passage.evidence_id,
                section=item.passage.section_number,
                section_title=item.passage.section_title,
                page=item.passage.page_number,
                quote=item.quote,
                law_name=item.passage.law_name,
                jurisdiction=item.passage.jurisdiction,
                source_url=item.passage.source_url,
            )
            for item in validated
        ]
        primary = citations[0]
        return AskResponse(
            supported=True,
            answer=answer,
            quote=primary.quote,
            section=primary.section,
            page=primary.page,
            evidence_id=primary.evidence_id,
            citations=citations,
            meta=meta,
        )

    def _build_meta(self, retrieval: _Retrieval) -> AskMeta | None:
        if not self.settings.debug:
            return None
        return AskMeta(
            strategy=retrieval.strategy,
            retrievers=retrieval.retrievers,
            candidate_ids=[hit.evidence_id for hit in retrieval.hits],
            top_keyword_score=retrieval.top_keyword_score,
            top_vector_score=retrieval.top_vector_score,
            embedding_model=(
                self.embeddings.model_name if self.settings.embeddings_enabled else None
            ),
            llm_model=getattr(self.llm, "model", self.llm.name),
        )


def _best_score(hits: list[PassageHit]) -> float | None:
    return max((hit.score for hit in hits), default=None)
