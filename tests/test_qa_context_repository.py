"""Publication guards and bounded reads, without connecting to a real database."""
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from types import SimpleNamespace as Row

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.qa_context_repository import ContextCatalog, ContextScopeError, QaContextRepository
from tests.qa_fixtures import identity
from tests.test_qa_context import INSTRUMENT, REVISION, graph


class Session:
    def __init__(self, results):
        self.results = deque(results)
        self.statements = []

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, statement):
        self.statements.append(statement)
        if str(statement).startswith("SET "):
            return Row()
        rows = self.results.popleft()
        return Row(all=lambda: rows, one_or_none=lambda: rows[0] if rows else None)

    async def scalars(self, statement):
        return await self.execute(statement)


class Database:
    def __init__(self, results):
        self.connection = Session(results)

    @asynccontextmanager
    async def session(self):
        yield self.connection


def published_row(**changes):
    catalog, _ = graph()
    item = catalog.items[INSTRUMENT]
    values = dict(vars(item), review_state="approved", revision_version=item.legal_version_id,
                  revision_hash=item.content_sha256, set_revision=REVISION,
                  set_hash=item.content_sha256, canonical_title=item.instrument_title,
                  code=item.jurisdiction_code, chunk_count=1, actual_chunks=1)
    return Row(**(values | changes))


def open_empty(repository):
    async def read():
        async with repository.snapshot(identity("publication"), instrument_ids=set()) as catalog:
            return catalog
    return asyncio.run(read())


@pytest.mark.parametrize("publication", [[], [Row(chunk_count=5, actual_chunks=4)]])
def test_inactive_or_incomplete_publication_is_rejected_before_context_reads(publication):
    db = Database([publication])
    with pytest.raises(ContextScopeError):
        open_empty(QaContextRepository(db))
    assert len(db.connection.statements) == 3


@pytest.mark.parametrize("changes", [
    {"review_state": "draft"}, {"revision_hash": "b" * 64},
    {"set_hash": "b" * 64}, {"set_revision": identity("other-revision")},
    {"revision_version": identity("other-version")},
])
def test_lightweight_metadata_still_checks_every_published_identity(changes):
    db = Database([[published_row(**changes)]])
    with pytest.raises(ContextScopeError, match="identity changed"):
        open_empty(QaContextRepository(db))


def test_proof_snapshot_reads_requested_ids_without_loading_the_graph():
    catalog, _ = graph()
    chunk = next(iter(catalog.chunks.values()))
    db = Database([[published_row()], [chunk]])
    async def read():
        async with QaContextRepository(db).proof_snapshot(identity("publication"),
                chunk_ids={chunk.id}, provision_ids=set()) as proof:
            assert set(proof.chunks) == {chunk.id}
            assert proof.nodes == {} and proof.references == {}
    asyncio.run(read())
    # Two transaction settings, publication/count plus metadata, then one ID
    # selection. No all-law provision, membership or reference queries.
    assert len(db.connection.statements) == 4
    query = db.connection.statements[-1].compile(dialect=postgresql.dialect())
    assert chunk.id in query.params["id_1"]
    assert "legal_chunks.chunk_set_id, legal_chunks.revision_id" in str(query)
    assert "legal_chunks.search_vector" not in str(query)
    metadata_query = str(db.connection.statements[2])
    assert "parser_version" not in metadata_query and "validation_report" not in metadata_query


def test_empty_publication_retains_the_no_pinned_sources_failure():
    db = Database([[Row(chunk_count=0, actual_chunks=0, instrument_id=None)]])
    with pytest.raises(ContextScopeError, match="no pinned sources"):
        open_empty(QaContextRepository(db))
    query = str(db.connection.statements[-1].compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN" in query and "count(*)" in query


def test_combined_metadata_validates_later_items_too():
    db = Database([[published_row(), published_row(
        instrument_id=identity("second-law"), review_state="draft")]])
    with pytest.raises(ContextScopeError, match="identity changed"):
        open_empty(QaContextRepository(db))


def graph_results(full, *, chunks=None, nodes=None):
    chunks = list(full.chunks.values()) if chunks is None else chunks
    nodes = list(full.nodes.values()) if nodes is None else nodes
    refs = [Row(**vars(ref), source_provision_id=source)
            for source, rows in full.references.items() for ref in rows]
    return [[published_row()],
            [(chunk, list(full.members.get(chunk.id, ()))) for chunk in chunks],
            nodes, refs]


def test_graph_batch_retains_split_sections_qualifiers_definitions_and_references():
    full, _ = graph(split=True, qualifier=True, definition=True, reference=True)
    db = Database(graph_results(full))
    async def read():
        async with QaContextRepository(db).snapshot(identity("publication"),
                instrument_ids={INSTRUMENT}) as catalog:
            assert catalog.items == full.items
            assert catalog.chunks == full.chunks and catalog.nodes == full.nodes
            assert dict(catalog.members) == dict(full.members)
            assert [row.chunk_text for row in catalog.chunks_by_root[
                (REVISION, identity("provision-1"))]] == [
                    row.chunk_text for row in full.chunks_by_root[
                        (REVISION, identity("provision-1"))]]
            for source, references in full.references.items():
                assert [vars(ref) | {"source_provision_id": source} for ref in references] == [
                    vars(ref) for ref in catalog.references[source]]
            before = len(db.connection.statements)
            await catalog.ensure_instruments({INSTRUMENT})
            assert len(db.connection.statements) == before
    asyncio.run(read())
    # Settings + one live metadata read + three graph reads, with all original data.
    assert len(db.connection.statements) == 6
    query = db.connection.statements[3].compile(dialect=postgresql.dialect())
    assert "array_agg(DISTINCT legal_chunk_provisions.provision_id)" in str(query)
    assert "legal_chunk_provisions.chunk_id = legal_chunks.id" in str(query)
    assert "legal_chunks.search_vector" not in str(query)


def test_law_without_chunks_still_loads_its_nodes_and_references():
    full, _ = graph(reference=True)
    db = Database(graph_results(full, chunks=[]))
    async def read():
        async with QaContextRepository(db).snapshot(identity("publication"),
                instrument_ids={INSTRUMENT}) as catalog:
            assert catalog.chunks == {} and dict(catalog.members) == {}
            assert catalog.nodes == full.nodes and catalog.references
    asyncio.run(read())


@pytest.mark.parametrize("bad_member", ["missing-node", "wrong-revision"])
def test_batched_membership_preserves_cross_revision_and_missing_node_rejection(bad_member):
    full, _ = graph()
    rows = graph_results(full)
    chunk, _ = rows[1][0]
    if bad_member == "missing-node":
        rows[1][0] = (chunk, [identity("missing-node")])
    else:
        root = full.nodes[chunk.root_provision_id]
        rows[2] = [Row(**(vars(node) | {"revision_id": identity("other-revision")}))
                   if node is root else node for node in rows[2]]
    db = Database(rows)
    async def read():
        async with QaContextRepository(db).snapshot(identity("publication"),
                instrument_ids={INSTRUMENT}):
            pass
    with pytest.raises(ContextScopeError, match="membership crosses"):
        asyncio.run(read())


def test_null_and_repeated_memberships_keep_the_original_set_semantics():
    full, _ = graph(split=True)
    rows = graph_results(full)
    first, members = rows[1][0]
    second, _ = rows[1][1]
    rows[1] = [(first, members * 2), (second, None)]
    db = Database(rows)
    async def read():
        async with QaContextRepository(db).snapshot(identity("publication"),
                instrument_ids={INSTRUMENT}) as catalog:
            assert catalog.members[first.id] == set(members)
            assert second.id not in catalog.members
            assert set(catalog.chunks) == {first.id, second.id}
    asyncio.run(read())


def test_lazy_loading_is_per_snapshot_and_rejects_unpublished_laws():
    full, _ = graph()
    loads = []
    async def loader(catalog, ids):
        loads.append(ids)
        catalog.chunks.update(full.chunks)
        catalog.nodes.update(full.nodes)
        catalog.members.update(full.members)
    catalog = ContextCatalog(None, full.items, {}, {}, defaultdict(set), defaultdict(list), loader=loader)
    async def read():
        await catalog.ensure_instruments({INSTRUMENT})
        await catalog.ensure_instruments({INSTRUMENT})
        assert catalog.chunks_by_root[(REVISION, identity("provision-1"))]
        with pytest.raises(ContextScopeError, match="unpublished"):
            await catalog.ensure_instruments({identity("unpublished")})
    asyncio.run(read())
    assert loads == [{INSTRUMENT}]


def test_chunk_sources_are_batched_and_not_reloaded_within_a_snapshot(monkeypatch):
    from app.repositories.legal_chunk_repository import LegalChunkRepository
    full, _ = graph(split=True)
    catalog = ContextCatalog(None, full.items, full.chunks, full.nodes, full.members, full.references)
    calls = []
    async def source_refs(session, chunks):
        calls.append({row.id for row in chunks})
        return {row.id: [{"mapping_quality": "exact"}] for row in chunks}
    monkeypatch.setattr(LegalChunkRepository, "_source_refs", source_refs)
    async def read():
        first = await catalog.chunk_sources(list(full.chunks))
        for key in full.chunks:
            assert await catalog.chunk_sources([key]) == {key: first[key]}
    asyncio.run(read())
    assert calls == [set(full.chunks)]
