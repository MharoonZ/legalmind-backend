"""Holds the snapshot of the active publication and keeps it current."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import monotonic

from app.retrieval.loader import active_identity, load_snapshot
from app.retrieval.snapshot import PublicationSnapshot, SnapshotError

logger = logging.getLogger(__name__)


class PublicationIndex:
    """One shared, atomically replaced snapshot per process.

    ``refresh`` costs one small query when nothing changed. A failed refresh
    keeps serving the snapshot that is already loaded; only a publication the
    database reports as active is ever loaded.
    """

    def __init__(self, db=None, *, cache_dir: str | None = None, refresh_seconds: float = 60.0):
        self._db = db
        self._cache_dir = cache_dir
        self._refresh_seconds = refresh_seconds
        self._snapshot: PublicationSnapshot | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._checked_at: float | None = None
        self._last_error: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: PublicationSnapshot | None) -> "PublicationIndex":
        """A fixed index for tests and offline tools."""
        index = cls()
        index._snapshot = snapshot
        return index

    @property
    def snapshot(self) -> PublicationSnapshot | None:
        return self._snapshot

    async def refresh(self, *, force: bool = False) -> bool:
        """Load the active publication if it differs from the one in memory."""
        if self._db is None:
            return False
        async with self._lock:
            try:
                current = self._snapshot
                if current is not None and not force:
                    identity = await active_identity(self._db)
                    if identity == (current.id, current.manifest_sha256):
                        self._checked_at, self._last_error = monotonic(), None
                        return False
                snapshot = await load_snapshot(self._db, cache_dir=self._cache_dir)
            except SnapshotError as exc:
                # An inconsistent publication must not be served, old or new.
                self._last_error = str(exc)
                self._snapshot = None
                logger.error("publication_snapshot_rejected reason=%s", exc)
                return False
            except Exception as exc:        # database or network trouble: keep what we have
                self._last_error = type(exc).__name__
                logger.warning("publication_refresh_failed error=%s", type(exc).__name__)
                return False
            self._checked_at, self._last_error = monotonic(), None
            changed = (snapshot is None) != (current is None) or (
                snapshot is not None and current is not None and
                (snapshot.id, snapshot.manifest_sha256) != (current.id, current.manifest_sha256))
            self._snapshot = snapshot
            if snapshot is None:
                logger.warning("publication_snapshot none_active")
            return changed or force

    async def ensure_loaded(self) -> PublicationSnapshot | None:
        if self._snapshot is None:
            await self.refresh()
        return self._snapshot

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            with suppress(Exception):
                await self.refresh()

    def start(self) -> None:
        if self._task is None and self._db is not None and self._refresh_seconds > 0:
            self._task = asyncio.create_task(self._watch(), name="publication-refresh")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def status(self) -> dict:
        snapshot = self._snapshot
        return {
            "loaded": snapshot is not None,
            **(snapshot.describe() if snapshot else {}),
            "last_error": self._last_error,
            "seconds_since_check": None if self._checked_at is None else round(monotonic() - self._checked_at, 1),
        }
