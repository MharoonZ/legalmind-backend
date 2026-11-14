"""Step 11: score upstream candidates without running retrieval or RRF again."""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from numbers import Real
from typing import Protocol

from app.core.qa_config import RerankerSettings, get_reranker_settings
from app.schemas.legal_search import LegalSearchOut
from app.schemas.qa_evidence import RerankResult, RerankedCandidate

logger = logging.getLogger(__name__)


class EvidenceContractError(ValueError):
    """The retrieved evidence cannot safely enter the answer pipeline."""


class RerankerUnavailable(RuntimeError):
    """A model/service failure, never an insufficient-evidence decision."""


class RerankerInputTooLong(EvidenceContractError):
    """The complete question and passage would be truncated."""


class PairScorer(Protocol):
    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]: ...


class FlagEmbeddingScorer:
    """One lazy model per configuration, with serialized loading and inference."""

    def __init__(self, settings: RerankerSettings, *, loader=None):
        self.settings = settings
        self._loader = loader
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._loader is not None:
            return self._loader()
        from huggingface_hub import snapshot_download
        from FlagEmbedding import FlagReranker
        # Startup must never begin an implicit multi-GB model download. Resolve
        # the installed snapshot locally; setup remains a separate explicit task.
        model_path = snapshot_download(self.settings.model_name, local_files_only=True)
        return FlagReranker(
            model_path,
            use_fp16=self.settings.use_fp16,
            devices=self.settings.device,
            trust_remote_code=False,
            query_max_length=self.settings.max_pair_tokens,
            max_length=self.settings.max_pair_tokens,
            normalize=False,
        )

    def load(self):
        """Preload the same singleton used for inference, without scoring a query."""
        with self._lock:
            if self._model is None:
                self._model = self._load()

    def score_pairs(self, pairs):
        if not pairs:
            return []
        with self._lock:
            if self._model is None:
                self._model = self._load()
            tokenizer = self._model.tokenizer
            for question, passage in pairs:
                tokens = tokenizer.encode(
                    question, passage, add_special_tokens=True, truncation=False,
                )
                if len(tokens) > self.settings.max_pair_tokens:
                    raise RerankerInputTooLong(
                        "Complete question/passage exceeds the reranker token budget; "
                        "no legal text was truncated."
                    )
            # Both limits are explicit: FlagEmbedding otherwise may truncate the
            # question independently of the full pair's token limit.
            scores = self._model.compute_score(
                pairs, batch_size=self.settings.batch_size,
                query_max_length=self.settings.max_pair_tokens,
                max_length=self.settings.max_pair_tokens, normalize=False,
            )
            if hasattr(scores, "tolist"):
                scores = scores.tolist()
            return [scores] if isinstance(scores, Real) else list(scores)


_scorers: dict[RerankerSettings, FlagEmbeddingScorer] = {}
_scorer_lock = threading.Lock()


def get_flag_scorer(settings: RerankerSettings) -> FlagEmbeddingScorer:
    with _scorer_lock:
        if settings not in _scorers:
            _scorers[settings] = FlagEmbeddingScorer(settings)
        return _scorers[settings]


async def warm_reranker(settings: RerankerSettings | None = None) -> bool:
    settings = settings or get_reranker_settings()
    if not settings.warm_on_startup:
        return False
    logger.info("Loading the cached answer reranker before accepting questions")
    try:
        await asyncio.to_thread(get_flag_scorer(settings).load)
    except Exception as exc:
        # Never print model/service exception details or substitute fake scores.
        logger.warning("Answer reranker warmup unavailable (%s)", type(exc).__name__)
        return False
    logger.info("Answer reranker ready; later questions reuse the loaded model")
    return True


class CandidateReranker:
    def __init__(self, settings: RerankerSettings | None = None, *, scorer: PairScorer | None = None):
        self.settings = settings or get_reranker_settings()
        self.scorer = scorer if scorer is not None else get_flag_scorer(self.settings)

    async def rerank(self, retrieval: LegalSearchOut) -> RerankResult:
        # Isolate our result from the upstream object, including nested metadata.
        snapshot = retrieval.model_copy(deep=True)
        base = dict(retrieval=snapshot, model_name=self.settings.model_name)
        if snapshot.status != "ok":
            if snapshot.results:
                raise EvidenceContractError("Non-success retrieval supplied candidates.")
            return RerankResult(**base)
        if not snapshot.results:
            return RerankResult(**base, warnings=("empty_successful_retrieval",))
        if snapshot.publication_id is None:
            raise EvidenceContractError("Candidate publication identity is missing.")
        identities = [item.chunk_id for item in snapshot.results]
        if len(set(identities)) != len(identities):
            raise EvidenceContractError("Duplicate candidate chunk identities.")
        version_scope = {}
        for item in snapshot.results:
            if not item.chunk_text.strip():
                raise EvidenceContractError("A candidate contains no source text.")
            scope = (item.legal_version_id, item.revision_id, item.chunk_set_id)
            if item.instrument_id in version_scope and version_scope[item.instrument_id] != scope:
                raise EvidenceContractError("One instrument has conflicting source versions.")
            version_scope[item.instrument_id] = scope
        if all(item.exact_rank is not None for item in snapshot.results):
            # These are already resolved by numbered provision and publication.
            # Keep all selected fragments so every requested root can expand.
            items = sorted(snapshot.results, key=lambda item: item.exact_rank)
            return RerankResult(**base, candidates=tuple(
                RerankedCandidate(publication_id=snapshot.publication_id, candidate=item,
                                  reranker_score=None, reranker_rank=rank)
                for rank, item in enumerate(items, 1)))
        pairs = [(snapshot.retrieval_query, item.chunk_text) for item in snapshot.results]
        try:
            raw_scores = await asyncio.to_thread(self.scorer.score_pairs, pairs)
            if len(raw_scores) != len(pairs) or any(isinstance(x, bool) for x in raw_scores):
                raise ValueError("Invalid score count or score type")
            scores = [float(value) for value in raw_scores]
            if not all(math.isfinite(value) for value in scores):
                raise ValueError("Non-finite model score")
        except RerankerInputTooLong:
            raise
        except Exception as exc:
            # Do not log questions, source text, model credentials, or a fabricated fallback.
            raise RerankerUnavailable("The cross-encoder could not score this request.") from exc
        indexed = list(zip(snapshot.results, scores, range(len(scores))))
        # Explicit reference fragments remain in their upstream order; arbitrary
        # relevance scores must not demote the provision the user asked to inspect.
        def ordering(row):
            candidate, score, original_index = row
            if candidate.exact_rank is not None:
                return (0, candidate.exact_rank, 0, original_index)
            return (1, -score, candidate.rank, original_index)
        indexed.sort(key=ordering)
        chosen = indexed[:self.settings.top_k]
        warnings = []
        if len(indexed) < self.settings.top_k:
            warnings.append("upstream_returned_fewer_candidates_than_top_k")
        return RerankResult(
            **base,
            candidates=tuple(
                RerankedCandidate(publication_id=snapshot.publication_id,
                    candidate=item, reranker_score=score, reranker_rank=rank)
                for rank, (item, score, _) in enumerate(chosen, 1)
            ),
            all_scores={item.chunk_id: score for item, score, _ in indexed},
            dropped_chunk_ids=tuple(item.chunk_id for item, _, _ in indexed[self.settings.top_k:]),
            warnings=tuple(warnings),
        )
