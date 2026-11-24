"""Build the long-lived services shared by every request."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.core.config import Settings
from app.db.session import Database
from app.embeddings.profiles import ACTIVE_PROFILE
from app.qa.llm import AnswerGenerator
from app.qa.rerank import CrossEncoderReranker
from app.qa.service import QuestionAnswerService
from app.retrieval.index import PublicationIndex
from app.retrieval.service import QueryEncoder, RetrievalService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppServices:
    index: PublicationIndex
    retrieval: RetrievalService
    reranker: CrossEncoderReranker
    generator: AnswerGenerator
    qa: QuestionAnswerService
    ask_slots: asyncio.Semaphore
    models_ready: bool = False
    _warmup: asyncio.Task | None = field(default=None, repr=False)

    async def warm(self) -> None:
        """Load the query encoder and reranker so the first question is fast."""
        snapshot = self.index.snapshot
        encoder_ready = await self.retrieval.encoder.warm(snapshot.profile if snapshot else ACTIVE_PROFILE)
        await self.reranker.warm()
        self.models_ready = encoder_ready
        if encoder_ready:
            logger.info("models_ready")
        else:
            logger.warning("query_encoder_unavailable using_keyword_results")

    def start_warmup(self) -> None:
        if self._warmup is None:
            self._warmup = asyncio.create_task(self.warm(), name="model-warmup")

    async def aclose(self) -> None:
        if self._warmup is not None:
            self._warmup.cancel()
            try:
                await self._warmup
            except (asyncio.CancelledError, Exception):
                pass
        await self.index.stop()
        await self.generator.aclose()


def build_services(db: Database, settings: Settings) -> AppServices:
    index = PublicationIndex(db, cache_dir=settings.publication_cache_dir or None,
                             refresh_seconds=settings.publication_refresh_seconds)
    retrieval = RetrievalService(index, settings, encoder=QueryEncoder())
    reranker = CrossEncoderReranker(settings)
    generator = AnswerGenerator(settings)
    recorder = None
    if settings.qa_record_runs:
        from app.qa.records import QaRecordRepository
        recorder = QaRecordRepository(db)
    qa = QuestionAnswerService(retrieval, reranker, generator, settings, recorder=recorder)
    return AppServices(index, retrieval, reranker, generator, qa,
                       asyncio.Semaphore(settings.qa_max_concurrent_requests))
