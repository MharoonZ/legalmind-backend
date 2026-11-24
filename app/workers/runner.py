"""Shared run loop for the background workers.

A worker keeps polling through transient database or network failures, and a
stop request (Ctrl+C or SIGTERM) cancels the job in progress so it can be
released immediately instead of waiting for its lease to expire.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 60.0


async def run_jobs(process_once: Callable[[], Awaitable[bool]], *, watch: bool, poll_seconds: float,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Process jobs until none remain, or keep polling when ``watch`` is set."""
    failures = 0
    while True:
        try:
            worked = await process_once()
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            if not watch:
                raise
            failures += 1
            delay = min(_MAX_BACKOFF_SECONDS, poll_seconds * 2 ** min(failures, 6))
            logger.exception("Worker iteration failed; retrying in %.0f s", delay)
            await sleep(delay)
            continue
        if not watch:
            return
        if not worked:
            await sleep(poll_seconds)


def run_until_stopped(main: Callable[[], Awaitable[None]]) -> None:
    """Run ``main`` and turn SIGTERM into the same clean cancellation as Ctrl+C."""
    async def supervised() -> None:
        task = asyncio.current_task()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        except (NotImplementedError, RuntimeError):     # Windows event loops
            pass
        await main()
    try:
        asyncio.run(supervised())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Worker stopped.")
