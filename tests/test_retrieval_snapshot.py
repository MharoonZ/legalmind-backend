"""The in-memory publication snapshot, its loader and its cache."""
import numpy as np
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
    assert snapshot.roots_between(law, "section", "4", "99") == []


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
    assert mask.sum() == 4 and snapshot.mask(None) is None
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
    chunks = [{"id": c.id, "instrument_id": c.instrument_id, "root_provision_id": c.root_provision_id,
               "ordinal": c.ordinal, "kind": c.kind, "label": c.label, "heading": c.heading,
               "reference_path": c.reference_path, "sequence_in_root": 1, "root_total_chunks": 1,
               "prefix": c.prefix, "chunk_text": c.text, "embedding": snapshot.vectors[c.index].tolist()}
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
    (lambda p, i, c, n, s: c[0].update(embedding=[0.1, 0.2]), "no vector"),
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
