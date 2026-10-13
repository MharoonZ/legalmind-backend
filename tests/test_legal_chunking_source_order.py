"""Positive/negative guards for source-ordered v2 chunking."""

from uuid import uuid4

import pytest

from app.services.legal_chunking import (
    ChunkConfig, ProvisionNode, ProvisionSourceSpan, build_chunks_from_spans,
)


def node(kind, label, text, ordinal, parent=None, status=None):
    return ProvisionNode(uuid4(), parent.id if parent else None, kind, label,
                         f"{kind}:{label}", ordinal, None, text, status)


def span(provision, start, end, block, offset=0, extraction=None):
    return ProvisionSourceSpan(uuid4(), provision.id, extraction or SOURCE,
                               0, block, offset, start, end, "exact")


SOURCE = uuid4()


def test_parent_continuation_follows_nested_list_in_physical_order():
    parent = node("section", "1", "1. Opening.\nClosing penalty.", 1)
    child = node("paragraph", "a", "(a) conduct;", 2, parent)
    spans = [span(parent, 0, 11, 1), span(child, 0, len(child.own_text), 2),
             span(parent, 11, len(parent.own_text), 3)]
    result = build_chunks_from_spans("Example Act", [parent, child], spans)
    chunk = result.chunks[0]
    assert chunk.chunk_text.index("(a) conduct;") < chunk.chunk_text.index("Closing penalty")
    assert [s.provision_id for s in chunk.segments] == [parent.id, child.id, parent.id]
    for segment in chunk.segments:
        source = parent if segment.provision_id == parent.id else child
        assert chunk.chunk_text[segment.chunk_char_start:segment.chunk_char_end] == \
            source.own_text[segment.provision_char_start:segment.provision_char_end]
    assert result.report["tokenizer"] is None  # never call character counts tokens


def test_omitted_and_note_nodes_do_not_enter_operative_search_text():
    section = node("section", "2", "2. Active law.", 1)
    note = node("other", "note", "Old historical wording.", 2)
    omitted = node("section", "3", "3. Repealed law.", 3, status="repealed")
    result = build_chunks_from_spans("Example Act", [section, note, omitted], [
        span(section, 0, len(section.own_text), 1),
        span(note, 0, len(note.own_text), 2),
        span(omitted, 0, len(omitted.own_text), 3),
    ])
    assert len(result.chunks) == 1
    assert "historical" not in result.chunks[0].search_text
    assert "Repealed" not in result.chunks[0].search_text


def test_missing_or_nonexact_source_span_blocks_build():
    section = node("section", "2", "2. Active law.", 1)
    with pytest.raises(ValueError, match="No source spans"):
        build_chunks_from_spans("Example Act", [section], [])
    bad = ProvisionSourceSpan(uuid4(), section.id, SOURCE, 0, 0, 0, 0,
                              len(section.own_text), "page_only")
    with pytest.raises(ValueError, match="Non-exact"):
        build_chunks_from_spans("Example Act", [section], [bad])


def test_large_source_fragment_is_visible_warning_not_silently_truncated():
    section = node("section", "5", "x" * 100, 1)
    result = build_chunks_from_spans("Act", [section], [
        span(section, 0, len(section.own_text), 1),
    ], ChunkConfig(max_chars=40))
    assert result.chunks[0].chunk_text.endswith(section.own_text)
    assert result.chunks[0].char_count > 40
    assert result.report["warnings"][0]["code"] == "oversized_source_fragment"


def test_same_revision_rerun_has_stable_keys_text_hashes_and_fragments():
    section = node("section", "19A", "19A. A rule.", 1)
    spans = [span(section, 0, len(section.own_text), 1)]
    one = build_chunks_from_spans("Act", [section], spans)
    two = build_chunks_from_spans("Act", [section], spans)
    assert one == two
    assert one.chunks[0].label == "19A"
    assert one.chunks[0].label != "19"


def test_long_provision_splits_without_losing_or_overlapping_its_text():
    section = node("section", "8", "x" * 80, 1)
    spans = [span(section, 0, 40, 1), span(section, 40, 80, 2)]
    result = build_chunks_from_spans("Act", [section], spans, ChunkConfig(max_chars=65))
    assert len(result.chunks) == 2
    assert [segment.provision_char_start for chunk in result.chunks for segment in chunk.segments] == [0, 40]
    assert [segment.provision_char_end for chunk in result.chunks for segment in chunk.segments] == [40, 80]
    assert result.chunks[0].root_provision_id == result.chunks[1].root_provision_id


def test_ambiguous_order_across_two_source_pdfs_refuses_a_guess():
    section = node("section", "8", "first second", 1)
    first = span(section, 0, 6, 1, extraction=uuid4())
    second = span(section, 6, 12, 1, extraction=uuid4())
    with pytest.raises(ValueError, match="Ambiguous order of multiple PDF sources"):
        build_chunks_from_spans("Act", [section], [first, second])
