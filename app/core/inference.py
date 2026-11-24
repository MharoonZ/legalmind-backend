"""Run CPU-bound model work off the event loop, one forward pass at a time.

Embedding and reranking share one worker thread so two PyTorch calls never
compete for the same cores. Waiting requests queue (bounded) instead of being
rejected; work whose request was cancelled before it started is skipped.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ModelBusy(RuntimeError):
    """The inference queue is full; the caller should degrade or retry."""


class InferenceCancelled(RuntimeError):
    """The request that asked for this work is gone."""


class InferenceRuntime:
    def __init__(self, *, max_pending: int = 64):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="legalmind-model")
        self._max_pending = max_pending
        self._pending = 0
        self._lock = threading.Lock()

    @property
    def pending(self) -> int:
        return self._pending

    async def run(self, operation: Callable[[threading.Event], T], *, stage: str) -> T:
        with self._lock:
            if self._pending >= self._max_pending:
                raise ModelBusy("Model inference queue is full.")
            self._pending += 1
        stop = threading.Event()
        submitted = perf_counter()

        def work() -> T:
            started = perf_counter()
            try:
                if stop.is_set():
                    raise InferenceCancelled()
                return operation(stop)
            finally:
                with self._lock:
                    self._pending -= 1
                logger.debug("model_work stage=%s queue_ms=%.0f run_ms=%.0f cancelled=%s", stage,
                             (started - submitted) * 1000, (perf_counter() - started) * 1000, stop.is_set())

        try:
            future = self._executor.submit(work)
        except BaseException:
            with self._lock:
                self._pending -= 1
            raise
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            stop.set()
            if future.cancel():             # never started: its finally block will not run
                with self._lock:
                    self._pending -= 1
            raise

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


runtime = InferenceRuntime()
