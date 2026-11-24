"""In-process sliding-window request limiter.

Protects a single API process from a runaway client. A deployment with more
than one process or host should also enforce limits at its gateway.
"""
from __future__ import annotations

from collections import deque
from time import monotonic

_MAX_TRACKED = 50_000


class RateLimiter:
    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, *, now: float | None = None) -> tuple[bool, float]:
        """Record one request. Returns ``(allowed, seconds_until_next_slot)``."""
        if limit <= 0:
            return True, 0.0
        current = monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and current - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= limit:
            return False, max(0.0, self._window - (current - hits[0]))
        hits.append(current)
        if len(self._hits) > _MAX_TRACKED:
            for stale in [name for name, seen in self._hits.items()
                          if not seen or current - seen[-1] >= self._window]:
                del self._hits[stale]
        return True, 0.0
