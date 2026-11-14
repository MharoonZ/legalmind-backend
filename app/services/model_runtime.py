"""Bound native model work, including work outliving a cancelled request."""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import perf_counter

logger = logging.getLogger(__name__)


class ModelBusy(RuntimeError):
    """The process has no remaining inference capacity."""


class InferenceCancelled(RuntimeError):
    """A native worker reached a cancellation checkpoint."""


class ModelRuntime:
    def __init__(self, *, capacity=2):
        # One native operation at a time avoids competing BGE forward passes
        # on CPU. Capacity includes the running operation and one waiting job.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="legalmind-model")
        self._slots = threading.BoundedSemaphore(capacity)

    async def run(self, operation, *, stage):
        if not self._slots.acquire(blocking=False):
            raise ModelBusy("Model capacity is occupied; retry shortly.")
        stop = threading.Event()
        submitted = perf_counter()

        def work():
            if stop.is_set():
                raise InferenceCancelled()
            started = perf_counter()
            try:
                return operation(stop)
            finally:
                logger.info("model_work stage=%s queue_ms=%.0f inference_ms=%.0f cancelled=%s",
                            stage, (started-submitted)*1000, (perf_counter()-started)*1000, stop.is_set())

        try:
            future = self._executor.submit(work)
        except BaseException:
            self._slots.release()
            raise
        # Release capacity when native work actually ends, never when its
        # awaiting HTTP coroutine times out. Queued cancelled work never runs.
        future.add_done_callback(lambda _: self._slots.release())
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            stop.set()
            future.cancel()
            raise

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)


runtime = ModelRuntime()


class RequestCapacity:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0

    @contextmanager
    def reserve(self, limit):
        with self._lock:
            admitted = self._active < limit
            if admitted:
                self._active += 1
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    self._active -= 1


request_capacity = RequestCapacity()
