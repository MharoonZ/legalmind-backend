"""Stage 11: reorder retrieved candidates with a cross-encoder, within a time budget.

A cross-encoder reads the question together with each passage, which is the
most accurate and the most expensive ranking signal. On a CPU-only host a
large reranker can take longer than the rest of the request combined, so the
stage measures its own throughput at start-up and, in ``auto`` mode, runs only
when the candidates can be scored inside ``qa_rerank_budget_ms``. Otherwise
the fused keyword + vector order is kept. A missing or failing model never
fails a question.
"""
from __future__ import annotations

import gc
import logging
import threading
from collections.abc import Callable, Sequence
from time import perf_counter

from app.core.inference import InferenceCancelled, InferenceRuntime, runtime as default_runtime
from app.retrieval.service import Candidate
from app.retrieval.snapshot import PublicationSnapshot

logger = logging.getLogger(__name__)

_MIN_USEFUL = 3              # reranking fewer passages than this cannot change much
_CALIBRATION_PAIRS = 2
_CALIBRATION_TEXT = ("Whoever contravenes the provisions of this section shall be punishable with "
                     "imprisonment for a term which may extend to one year, or with fine, or with both. ") * 4

Scorer = Callable[[str, Sequence[str]], Sequence[float]]


class CrossEncoderReranker:
    def __init__(self, settings, *, runtime: InferenceRuntime | None = None, scorer: Scorer | None = None,
                 tokens_per_second: float | None = None):
        self.settings = settings
        self._runtime = runtime or default_runtime
        self._scorer = scorer                 # injected in tests; bypasses the model
        self._tokenizer = None
        self._model = None
        self._lock = threading.Lock()
        self._unavailable = False
        self._over_budget = False             # measured too slow for "auto" on this host
        self.tokens_per_second = tokens_per_second

    # ------------------------------------------------------------- loading
    @property
    def enabled(self) -> bool:
        return self.settings.qa_rerank_mode != "off" and not self._unavailable and not self._over_budget

    def _load(self) -> None:
        with self._lock:
            if self._model is not None or self._unavailable:
                return
            try:
                from transformers import AutoModelForSequenceClassification, AutoTokenizer
                name = self.settings.qa_rerank_model_name
                # Local files only: a server must not start a multi-GB download
                # on its first question. `python -m scripts.pipeline.download_models`
                # fetches the configured models explicitly.
                self._tokenizer = AutoTokenizer.from_pretrained(name, local_files_only=True)
                model = AutoModelForSequenceClassification.from_pretrained(name, local_files_only=True)
                model.eval().to(self.settings.qa_rerank_device)
                if self.settings.qa_rerank_use_fp16 and self.settings.qa_rerank_device != "cpu":
                    model.half()
                self._model = model
            except Exception as exc:
                self._unavailable = True
                logger.warning("reranker_unavailable model=%s error=%s (fused ranking will be used)",
                               self.settings.qa_rerank_model_name, type(exc).__name__)

    def _forward(self, query: str, passages: Sequence[str], stop: threading.Event | None = None,
                 ) -> tuple[list[float], int]:
        import torch
        encoded = self._tokenizer(
            [query] * len(passages), list(passages), truncation="only_second",
            max_length=self.settings.qa_rerank_max_pair_tokens, padding=False)
        lengths = [len(ids) for ids in encoded["input_ids"]]
        order = sorted(range(len(passages)), key=lambda i: lengths[i], reverse=True)
        scores = [0.0] * len(passages)
        size = self.settings.qa_rerank_batch_size
        with torch.inference_mode():
            for offset in range(0, len(order), size):
                if stop is not None and stop.is_set():
                    raise InferenceCancelled()
                batch = order[offset:offset + size]
                padded = self._tokenizer.pad(
                    {key: [encoded[key][i] for i in batch] for key in ("input_ids", "attention_mask")},
                    padding=True, return_tensors="pt").to(self.settings.qa_rerank_device)
                logits = self._model(**padded).logits.view(-1).float().cpu().tolist()
                for index, value in zip(batch, logits):
                    scores[index] = float(value)
        return scores, sum(lengths)

    def _calibrate(self) -> None:
        passages = [_CALIBRATION_TEXT] * _CALIBRATION_PAIRS
        self._forward("What is the punishment?", passages)        # first pass pays one-off costs
        started = perf_counter()
        _, tokens = self._forward("What is the punishment?", passages)
        self.tokens_per_second = tokens / max(perf_counter() - started, 1e-6)

    async def warm(self) -> None:
        """Load the model and measure how fast this host can score passages."""
        if self.settings.qa_rerank_mode == "off" or self._scorer is not None:
            return

        def work(_stop):
            self._load()
            if self._model is not None and self.tokens_per_second is None:
                self._calibrate()

        try:
            await self._runtime.run(work, stage="reranker_warmup")
        except Exception as exc:
            self._unavailable = True
            logger.warning("reranker_warmup_failed error=%s", type(exc).__name__)
            return
        if self._model is not None:
            budget = self.settings.qa_rerank_budget_ms / 1000 * (self.tokens_per_second or 0)
            affordable = budget / self.settings.qa_rerank_max_pair_tokens
            logger.info(
                "reranker_ready model=%s mode=%s tokens_per_second=%.0f passages_in_budget=%.1f%s",
                self.settings.qa_rerank_model_name, self.settings.qa_rerank_mode,
                self.tokens_per_second or 0, affordable,
                "" if self.settings.qa_rerank_mode == "on" or affordable >= _MIN_USEFUL else
                " (too slow on this host: fused ranking will be used; set QA_RERANK_MODE=on to force)")
            if self.settings.qa_rerank_mode == "auto" and affordable < _MIN_USEFUL:
                # It will never run here: do not keep gigabytes of weights in memory.
                with self._lock:
                    self._model = self._tokenizer = None
                self._over_budget = True
                gc.collect()

    # ------------------------------------------------------------- ranking
    def _affordable(self, count: int) -> int:
        """How many of ``count`` passages fit the latency budget on this host."""
        if self.settings.qa_rerank_mode == "on" or self._scorer is not None:
            return count
        if not self.tokens_per_second:
            return 0
        budget_tokens = self.settings.qa_rerank_budget_ms / 1000 * self.tokens_per_second
        return min(count, int(budget_tokens // self.settings.qa_rerank_max_pair_tokens))

    async def rerank(self, query: str, candidates: Sequence[Candidate],
                     snapshot: PublicationSnapshot) -> tuple[list[Candidate], str]:
        """Return candidates best-first and the method that ordered them."""
        ordered = list(candidates)
        if not ordered or all(item.exact_rank is not None for item in ordered):
            return ordered, "exact"         # the user named these provisions; keep their order
        if not self.enabled:
            return ordered, "fused"
        head = ordered[:self.settings.qa_rerank_candidates]
        count = self._affordable(len(head))
        if count < min(_MIN_USEFUL, len(head)):
            return ordered, "fused"
        head, tail = ordered[:count], ordered[count:]
        passages = [self._passage(item, snapshot) for item in head]
        started = perf_counter()
        try:
            if self._scorer is not None:
                scores = list(self._scorer(query, passages))
            else:
                def work(stop):
                    self._load()
                    if self._model is None:
                        raise RuntimeError("reranker model unavailable")
                    return self._forward(query, passages, stop)[0]
                scores = await self._runtime.run(work, stage="reranking")
            if len(scores) != len(head) or any(value != value for value in scores):
                raise ValueError("invalid reranker scores")
        except Exception as exc:
            logger.warning("rerank_failed error=%s (fused ranking kept)", type(exc).__name__)
            return ordered, "fused"
        for item, score in zip(head, scores):
            item.reranker_score = float(score)
        head.sort(key=lambda item: (-item.reranker_score, item.rank))
        logger.info("rerank passages=%d elapsed_ms=%.0f", len(head), (perf_counter() - started) * 1000)
        return head + tail, "cross_encoder"

    @staticmethod
    def _passage(candidate: Candidate, snapshot: PublicationSnapshot) -> str:
        chunk = candidate.chunk
        title = snapshot.instruments[chunk.instrument_id].title
        heading = f" — {chunk.heading}" if chunk.heading else ""
        return f"{title} — {chunk.citation}{heading}\n{snapshot.display[chunk.index]}"
