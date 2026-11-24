"""Read-only integration tests against a real PostgreSQL database.

Skipped unless ``LEGALMIND_TEST_DATABASE_URL`` points at a database that has
the migrations applied and an active publication (Phase A and B have run):

    LEGALMIND_TEST_DATABASE_URL=postgresql://user:pass@host:5432/db pytest tests/test_integration_postgres.py

Nothing is written. The tests read the published corpus exactly as the API
does, and then exercise retrieval on whatever laws that publication holds, so
they are valid for any corpus. TLS is used unless the host is local; set
``LEGALMIND_TEST_DB_SSL=true|false`` to override.
"""
import asyncio
import os

import numpy as np
import pytest
from sqlalchemy import text

from app.core.inference import InferenceRuntime
from app.retrieval.index import PublicationIndex
from app.retrieval.service import QueryEncoder, RetrievalService
from tests.corpus import settings as retrieval_settings

URL = os.environ.get("LEGALMIND_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="LEGALMIND_TEST_DATABASE_URL is not set")


def database():
    from app.core.config import Settings
    from app.db.session import Database

    local = any(host in URL for host in ("@127.0.0.1", "@localhost", "@[::1]"))
    ssl = os.environ.get("LEGALMIND_TEST_DB_SSL", "false" if local else "true").lower() == "true"
    return Database(Settings(database_url=URL, db_ssl=ssl, db_connect_timeout=30, db_connect_retries=1))


def run(work):
    """Run ``work(db)`` on its own event loop with its own connection pool."""
    async def main():
        db = database()
        try:
            assert await db.connect(), "the test database is unreachable"
            return await work(db)
        finally:
            await db.dispose()
    return asyncio.run(main())


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    """The active publication loaded three ways, with the database's own counts."""
    from app.retrieval.loader import load_snapshot

    cache = str(tmp_path_factory.mktemp("publication-cache"))

    async def work(db):
        async with db.session() as session:
            row = (await session.execute(text(
                "SELECT p.id, p.manifest_sha256, count(i.chunk_set_id) AS instrument_count, "
                "coalesce(sum(i.chunk_count), 0) AS chunk_count FROM corpus_publications p "
                "LEFT JOIN publication_items i ON i.publication_id = p.id "
                "WHERE p.status = 'active' GROUP BY p.id, p.manifest_sha256"))).one_or_none()
        if row is None:
            return None
        direct = await load_snapshot(db)
        await load_snapshot(db, cache_dir=cache)            # writes the cache
        cached = await load_snapshot(db, cache_dir=cache)   # reads it back
        return row, direct, cached

    result = run(work)
    if result is None:
        pytest.skip("the test database has no active publication")
    return result


def test_the_snapshot_holds_exactly_what_was_published(published):
    row, snapshot, _ = published
    assert snapshot.id == row.id and snapshot.manifest_sha256 == row.manifest_sha256
    assert len(snapshot.chunks) == row.chunk_count and len(snapshot.instruments) == row.instrument_count
    assert snapshot.vectors.shape == (row.chunk_count, snapshot.profile.dimension)
    assert np.allclose(np.linalg.norm(snapshot.vectors, axis=1), 1.0, atol=1e-4)
    for chunk in snapshot.chunks:
        assert chunk.segments, f"chunk {chunk.id} has no source span"
        assert chunk.text.startswith(chunk.prefix)
        for segment in chunk.segments:
            assert 0 <= segment.start < segment.end <= len(chunk.text)
            assert segment.provision_id in snapshot.nodes and segment.page_index >= 0
    assert all(snapshot.chunks_by_root[key] for keys in snapshot.root_order.values() for key in keys)


def test_the_disk_cache_reproduces_the_database_load(published):
    _, direct, cached = published
    assert cached.id == direct.id and cached.manifest_sha256 == direct.manifest_sha256
    assert [chunk.id for chunk in cached.chunks] == [chunk.id for chunk in direct.chunks]
    assert [chunk.text for chunk in cached.chunks] == [chunk.text for chunk in direct.chunks]
    assert [chunk.segments for chunk in cached.chunks] == [chunk.segments for chunk in direct.chunks]
    assert np.allclose(cached.vectors, direct.vectors, atol=1e-6)      # re-normalised on load
    assert cached.referrers == direct.referrers and cached.outgoing == direct.outgoing


def _numbered_root(snapshot):
    """A law and one of its numbered sections, whichever corpus is published."""
    for law in sorted(snapshot.instruments.values(), key=lambda item: item.title):
        if sum(other.title == law.title for other in snapshot.instruments.values()) > 1:
            continue
        for key in snapshot.root_order[law.id]:
            first = snapshot.chunks_by_root[key][0]
            if first.kind == "section" and (first.label or "").isdigit():
                return law, first
    pytest.skip("the publication has no numbered section")


def _misspell(title: str) -> str:
    """Drop one inner letter from the longest word of a title."""
    longest = max(title.split(), key=len)
    middle = len(longest) // 2
    return title.replace(longest, longest[:middle] + longest[middle + 1:], 1)


def test_a_named_section_is_found_with_and_without_a_spelling_mistake(published):
    _, snapshot, _ = published
    law, section = _numbered_root(snapshot)

    def unavailable(_text, _profile):
        raise RuntimeError("no embedding model in this test")
    service = RetrievalService(PublicationIndex.from_snapshot(snapshot), retrieval_settings(),
                               encoder=QueryEncoder(runtime=InferenceRuntime(), encode=unavailable))
    for title in (law.title, _misspell(law.title)):
        result = asyncio.run(service.retrieve(f"What is section {section.label} of {title}?"))
        assert result.status == "ok", (title, result.message)
        assert result.candidates[0].chunk.root_provision_id == section.root_provision_id
        assert result.candidates[0].chunk.instrument_id == law.id
    # Keyword search alone (no embedding model) still returns real passages.
    topic = " ".join((section.heading or section.text[section.body_start:]).split()[:8])
    found = asyncio.run(service.retrieve(topic))
    assert found.status == "ok" and found.candidates
    assert any("keyword search only" in warning for warning in found.warnings)


def test_chunk_repository_reads_return_chunks_with_their_sources(published):
    from app.repositories.legal_chunk_repository import LegalChunkRepository

    _, snapshot, _ = published
    law, section = _numbered_root(snapshot)

    async def work(db):
        repository = LegalChunkRepository(db)
        by_label = await repository.find_by_label(law.chunk_set_id, section.label, kind="section")
        by_path = await repository.find_by_reference_path(law.chunk_set_id, section.reference_path)
        return by_label, by_path

    by_label, by_path = run(work)
    assert by_label and by_path
    assert {row["id"] for row in by_label} >= {section.id} and by_path[0]["reference_path"] == section.reference_path
    assert all(row["source_refs"] for row in by_label)
