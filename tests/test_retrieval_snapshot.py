"""The in-memory publication snapshot, its loader and its cache."""
import numpy as np
from pgvector import Vector
import pytest

from app.retrieval import loader
from app.retrieval.snapshot import PublicationSnapshot, SnapshotError, readable
from tests.corpus import CONSUMER, GAMBLING, PRISONS, PROFILE, law_id, make_snapshot


@pytest.fixture(scope="module")
def snapshot():
    return make_snapshot()


def test_readable_text_drops_pdf_wrapping_and_footnote_numbers():
    raw = "extend to \n\n1[three years], or with fine which may extend to 2[five thousand rupees]"
    assert readable(raw) == "extend to [three years], or with fine which may extend to [five thousand rupees]"
    # A number that is itself the provision referred to is never removed.
    assert readable("under section 3[59]. and rule 5[A]") == "under section 3[59]. and rule 5[A]"
    assert readable("fine of one thousand rupees, 8[* * *] and") == "fine of one thousand rupees, [* * *] and"


def test_labels_resolve_without_hyphens_and_with_the_wrong_kind_word(snapshot):
    law = law_id(GAMBLING)
    assert snapshot.chunks_by_root[snapshot.find_root(law, "section", "5a")][0].label == "5-A"
    # Users say "article" or "rule" for sections; the label still identifies one provision.
    assert snapshot.find_root(law, "article", "5") == snapshot.find_root(law, "section", "5")
    assert snapshot.find_root(law, "section", "99") is None
    assert snapshot.find_root(law, "schedule", "5") is None


def test_ranges_follow_document_order_and_include_lettered_sections(snapshot):
    law = law_id(GAMBLING)
    labels = [snapshot.chunks_by_root[key][0].label for key in snapshot.roots_between(law, "section", "4", "8")]
    assert labels == ["4", "5", "5-A", "8"]
    assert snapshot.roots_between(law, "section", "8", "4") == snapshot.roots_between(law, "section", "4", "8")
    # An end the law does not have (omitted or never enacted) still returns what lies between.
    open_ended = snapshot.roots_between(law, "section", "4", "99")
    assert [snapshot.chunks_by_root[key][0].label for key in open_ended] == ["4", "5", "5-A", "8"]
    assert [snapshot.chunks_by_root[key][0].label
            for key in snapshot.roots_between(law, "section", "6", "8")] == ["8"]
    assert snapshot.roots_between(law, "section", "90", "99") == []


def test_child_paths_breadcrumbs_and_definition_terms(snapshot):
    law = law_id(GAMBLING)
    _, root = snapshot.find_root(law, "section", "3")
    assert snapshot.child_path(root, ["1", "a"]) == "f:test/s:3/sub:1/p:a"
    assert snapshot.child_path(root, ["7"]) is None
    key, terms = snapshot.definitions[law]
    assert snapshot.chunks_by_root[key][0].label == "2"
    assert ("common", "game", "hous") in terms and ("game",) in terms
    assert law_id(PRISONS) in snapshot.definitions
    assert snapshot.labels_of(law)[:3] == ["1", "2", "3"]
    assert snapshot.describe()["laws"] == 5


def test_masks_select_laws_and_display_text_excludes_the_prefix(snapshot):
    mask = snapshot.mask([law_id(CONSUMER)])
    assert mask.sum() == 5 and snapshot.mask(None) is None
    chunk = snapshot.chunks_by_root[snapshot.find_root(law_id(GAMBLING), "section", "5")][0]
    assert snapshot.display[chunk.index].startswith("5. Penalty for gaming in a public place.")
    assert "[three years]" in snapshot.display[chunk.index] and "1[three" not in snapshot.display[chunk.index]
    assert chunk.citation == "Section 5" and chunk.text[chunk.body_start:].startswith("5. Penalty")


def test_inconsistent_snapshots_are_rejected(snapshot):
    arguments = dict(publication_id=snapshot.id, name="x", manifest_sha256="b" * 64, profile=PROFILE,
                     instruments=snapshot.instruments.values(), chunks=snapshot.chunks,
                     nodes=snapshot.nodes.values())
    with pytest.raises(SnapshotError, match="vectors"):
        PublicationSnapshot(**arguments, vectors=np.zeros((2, PROFILE.dimension)))
    broken = np.array(snapshot.vectors)
    broken[0] = 0
    with pytest.raises(SnapshotError, match="zero"):
        PublicationSnapshot(**arguments, vectors=broken)
    broken[0, 0] = np.nan
    with pytest.raises(SnapshotError, match="finite"):
        PublicationSnapshot(**arguments, vectors=broken)
    with pytest.raises(SnapshotError, match="unpublished"):
        PublicationSnapshot(**{**arguments, "instruments": []}, vectors=snapshot.vectors)


def test_cache_round_trip_reproduces_the_snapshot(tmp_path, snapshot):
    loader._write_cache(str(tmp_path), snapshot)
    restored = loader._read_cache(str(tmp_path), snapshot.id, snapshot.manifest_sha256)
    assert restored is not None and restored.chunks == snapshot.chunks
    assert restored.instruments == snapshot.instruments and restored.nodes == snapshot.nodes
    assert np.allclose(restored.vectors, snapshot.vectors, atol=1e-6)
    # A different manifest, a missing directory or a corrupt file is a cache miss.
    assert loader._read_cache(str(tmp_path), snapshot.id, "c" * 64) is None
    assert loader._read_cache(None, snapshot.id, snapshot.manifest_sha256) is None
    for path in tmp_path.iterdir():
        if path.suffix == ".gz":
            path.write_bytes(b"not gzip")
    assert loader._read_cache(str(tmp_path), snapshot.id, snapshot.manifest_sha256) is None


def _rows(snapshot):
    publication = {"id": snapshot.id, "name": snapshot.name, "manifest_sha256": snapshot.manifest_sha256,
                   "chunk_count": len(snapshot.chunks), "profile_id": "p", "profile_name": PROFILE.name,
                   "model_id": PROFILE.model_id, "dimension": PROFILE.dimension,
                   "max_tokens": PROFILE.max_tokens, "input_recipe": PROFILE.input_recipe, "normalize": True}
    items = [{"instrument_id": law.id, "legal_version_id": law.legal_version_id, "revision_id": law.revision_id,
              "chunk_set_id": law.chunk_set_id, "content_sha256": "d" * 64, "canonical_title": law.title,
              "citation_key": law.citation_key, "aliases": ["Alias", 7], "instrument_type": law.instrument_type,
              "jurisdiction_code": law.jurisdiction_code, "edition_key": law.edition_key,
              "applicability_status": law.applicability_status, "review_state": "approved",
              "revision_hash": "d" * 64, "revision_version": law.legal_version_id, "set_hash": "d" * 64,
              "set_revision": law.revision_id} for law in snapshot.instruments.values()]
    for item in items:
        count = sum(c.instrument_id == item["instrument_id"] for c in snapshot.chunks)
        item.update(pinned_count=count, current_count=count, embedded_count=count)
    chunks = [{"id": c.id, "instrument_id": c.instrument_id, "root_provision_id": c.root_provision_id,
               "ordinal": c.ordinal, "kind": c.kind, "label": c.label, "heading": c.heading,
               "reference_path": c.reference_path, "sequence_in_root": 1, "root_total_chunks": 1,
               "prefix": c.prefix, "chunk_text": c.text, "embedding": Vector(snapshot.vectors[c.index]).to_binary()}
              for c in snapshot.chunks]
    nodes = [{"id": n.id, "instrument_id": n.instrument_id, "parent_id": n.parent_id, "kind": n.kind,
              "label": n.label, "heading": n.heading, "reference_path": n.reference_path,
              "ordinal": n.ordinal, "own_text": n.text, "source_status": n.source_status}
             for n in snapshot.nodes.values()]
    segments = [{"chunk_id": c.id, "chunk_char_start": s.start, "chunk_char_end": s.end,
                 "provision_char_start": 0, "provision_char_end": s.end - s.start,
                 "provision_id": s.provision_id, "source_span_id": s.source_span_id, "kind": s.kind,
                 "label": s.label, "reference_path": s.reference_path, "provision_text_sha256": "e" * 64,
                 "mapping_quality": "exact", "extraction_id": s.file_id, "page_id": s.file_id,
                 "block_id": s.file_id, "page_index": s.page_index, "block_text_sha256": "f" * 64,
                 "file_id": s.file_id, "file_sha256": s.file_sha256, "original_filename": s.filename,
                 "source_url": None, "publisher": None} for c in snapshot.chunks for s in c.segments]
    return publication, items, chunks, nodes, segments


def test_database_rows_assemble_into_an_equivalent_snapshot(snapshot):
    built = loader._assemble(*_rows(snapshot))
    np.testing.assert_allclose(built.vectors, snapshot.vectors, atol=1e-6)
    assert [c.text for c in built.chunks] == [c.text for c in snapshot.chunks]
    assert all(len(c.segments) == 2 for c in built.chunks)
    assert next(iter(built.instruments.values())).aliases == ("Alias",)
    assert built.find_root(law_id(GAMBLING), "section", "5a") is not None


@pytest.mark.parametrize("damage,message", [
    (lambda p, i, c, n, s: i.clear(), "no pinned sources"),
    (lambda p, i, c, n, s: i[0].update(review_state="draft"), "approved revision"),
    (lambda p, i, c, n, s: i[0].update(set_hash="0" * 64), "approved revision"),
    (lambda p, i, c, n, s: c.pop(), "chunk count"),
    (lambda p, i, c, n, s: c[0].update(embedding=None), "no vector"),
    (lambda p, i, c, n, s: c[0].update(embedding=Vector([0.1, 0.2]).to_binary()), "no vector"),
])
def test_incomplete_or_changed_publications_fail_closed(snapshot, damage, message):
    rows = _rows(snapshot)
    damage(*rows)
    with pytest.raises(SnapshotError, match=message):
        loader._assemble(*rows)


def test_non_exact_source_spans_are_not_used_for_citations(snapshot):
    publication, items, chunks, nodes, segments = _rows(snapshot)
    for row in segments[:2]:
        row["mapping_quality"] = "approximate"
    built = loader._assemble(publication, items, chunks, nodes, segments)
    assert built.chunks[0].segments == () and len(built.chunks[1].segments) == 2


def test_cross_references_link_provisions_in_both_directions(snapshot):
    consumer, gambling = law_id(CONSUMER), law_id(GAMBLING)

    def root(law, label):
        return snapshot.find_root(law, "section", label.replace("-", "").lower())

    # "as provided in section 4": a forward reference, and its reverse.
    assert snapshot.outgoing[root(consumer, "25")] == (root(consumer, "4"),)
    # The penalty clause names "sections 4 to 8, ... 18 to 22": it refers to 4 and 19, not to 25 or 2.
    assert set(snapshot.outgoing[root(consumer, "32")]) == {root(consumer, "4"), root(consumer, "19")}
    assert snapshot.referrers[root(consumer, "19")] == (root(consumer, "32"),)
    assert snapshot.referrers[root(consumer, "4")] == (root(consumer, "25"), root(consumer, "32"))
    assert snapshot.referrers[root(gambling, "5")] == (root(gambling, "5-A"),)
    assert root(consumer, "2") not in snapshot.referrers
    # A provision never refers to itself, and numbers are resolved inside the same law only.
    assert all(key not in targets and all(target[0] == key[0] for target in targets)
               for key, targets in snapshot.outgoing.items())


def test_published_foreign_references_do_not_link_to_namesake_provisions(monkeypatch):
    from tests.corpus import LAWS
    title = "Licensing Rules"
    monkeypatch.setitem(LAWS, title, [
        ("rule", "5", "Licences", "Issue a licence."),
        ("rule", "9", "Fees", "A fee is payable."),
        ("rule", "10", "Appeals", "Apply section 5 of the Act. Also see rule 5 of these Rules."),
        ("schedule", None, "Table", "Old Rules, 1878 Rule 9; Parent Act, 1878 Section 5.")])
    snap = make_snapshot()
    def key(kind, label):
        return snap.find_root(law_id(title), kind, label)

    assert snap.outgoing.get(key("schedule", ""), ()) == ()
    assert snap.outgoing[key("rule", "10")] == (key("rule", "5"),)


def test_display_removes_identifiable_fused_footnotes_only():
    from app.retrieval.snapshot import readable
    assert readable("Code of Procedure, 18988, as amended.") == "Code of Procedure, 1898, as amended."
    assert readable("five persons1[.]") == "five persons[.]"
    assert readable("a fine of 18988 rupees, section 3[59]") == "a fine of 18988 rupees, section 3[59]"


@pytest.mark.parametrize("which", ["metadata", "vectors"])
def test_a_truncated_cache_discards_both_files(tmp_path, snapshot, which):
    loader._write_cache(str(tmp_path), snapshot)
    meta, vectors = loader._cache_paths(str(tmp_path), snapshot.id, snapshot.manifest_sha256)
    damaged = meta if which == "metadata" else vectors
    damaged.write_bytes(damaged.read_bytes()[:12])
    assert loader._read_cache(str(tmp_path), snapshot.id, snapshot.manifest_sha256) is None
    assert not meta.exists() and not vectors.exists()


def test_cache_files_are_synced_before_replace_and_failed_writes_clean_up(tmp_path, snapshot, monkeypatch):
    synced = []
    monkeypatch.setattr(loader.os, "fsync", lambda fd: synced.append(fd))

    def fail(*args):
        assert synced
        raise OSError("destination unavailable")

    monkeypatch.setattr(loader.os, "replace", fail)
    loader._write_cache(str(tmp_path), snapshot)
    assert synced and not list(tmp_path.iterdir())


def test_cache_prunes_only_older_publication_files(tmp_path):
    from uuid import uuid4
    keep = tmp_path / "operator-notes.txt"
    keep.write_text("keep")
    for _ in range(4):
        snapshot = make_snapshot()
        snapshot.id = uuid4()
        loader._write_cache(str(tmp_path), snapshot)
    assert keep.is_file() and len(list(tmp_path.glob("*.json.gz"))) <= 2
    assert loader._read_cache(str(tmp_path), snapshot.id, snapshot.manifest_sha256)


def test_load_checks_approved_sources_and_builds_off_the_event_loop(tmp_path, snapshot, monkeypatch):
    import asyncio
    import threading
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    pub, items, chunks, nodes, segments = _rows(snapshot)
    responses = {loader._ACTIVE: [pub], loader._ITEMS: items, loader._CHUNKS: chunks,
                 loader._NODES: nodes, loader._SEGMENTS: segments}
    queries, workers = [], []

    async def execute(query, *args):
        assert query in (loader._ACTIVE, loader._ITEMS)
        rows = responses[query]
        queries.append(query)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: rows[0], all=lambda: rows))

    async def stream(query, params, *, execution_options):
        assert 0 < execution_options["yield_per"] <= 256
        queries.append(query)

        async def mappings():
            for row in responses[query]:
                yield row

        async def close():
            pass

        return SimpleNamespace(mappings=mappings, close=close)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=execute, stream=stream)

    def observed(f):
        def call(*args, **kwargs):
            workers.append(threading.get_ident())
            return f(*args, **kwargs)
        return call

    for name in ("_read_cache", "_assemble", "_write_cache"):
        monkeypatch.setattr(loader, name, observed(getattr(loader, name)))
    db = SimpleNamespace(session=session)
    async def run():
        assert await loader.load_snapshot(db, cache_dir=str(tmp_path))
        assert threading.get_ident() not in workers
        queries.clear()
        assert await loader.load_snapshot(db, cache_dir=str(tmp_path))
        assert loader._ITEMS in queries and loader._CHUNKS not in queries
        items[0]["review_state"] = "draft"
        with pytest.raises(SnapshotError, match="approved"):
            await loader.load_snapshot(db, cache_dir=str(tmp_path))
        items[0]["review_state"] = "approved"
        items[0]["embedded_count"] -= 1
        with pytest.raises(SnapshotError):
            await loader.load_snapshot(db, cache_dir=str(tmp_path))
    asyncio.run(run())


def test_display_separates_prose_footnotes_in_legal_titles_and_sentences():
    from app.retrieval.snapshot import readable

    source = ("Under the Code of Civil Procedure1, the Government may constitute11 an office "
              "and make rules2, on such date2 as it prescribes. Under the Northern District4 "
              "Protection Act the Tribunal10 may act.")
    assert readable(source) == ("Under the Code of Civil Procedure, the Government may constitute an office "
                                "and make rules, on such date as it prescribes. Under the Northern District "
                                "Protection Act the Tribunal may act.")
    identifiers = "Use form Code4, reference HAL12 and model Delta2 under section 3[59]."
    assert readable(identifiers) == identifiers
