"""Pure-function tests for the legal-aware chunker (no database required).

Mirrors the style of tests/test_legal_structure.py: provisions are built by
hand as plain dataclasses, and assertions check the deterministic, inspect-
able output rather than any persisted state.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.legal_chunking import (
    ChunkConfig, ProvisionNode, build_chunks, build_prefix,
)

TITLE = "The Example Act, 2026"


def _node(kind, label, heading, own_text, ordinal, parent=None, source_status=None):
    return ProvisionNode(
        id=uuid4(), parent_id=parent.id if parent else None, kind=kind, label=label,
        reference_path=f"f:deadbeef/{kind}:{label or ordinal}", ordinal=ordinal,
        heading=heading, own_text=own_text, source_status=source_status,
    )


# --------------------------------------------------------------------- basics

def test_empty_input_produces_no_chunks():
    result = build_chunks(TITLE, [])
    assert result.chunks == []
    assert result.report["chunk_count"] == 0
    assert result.report["total_provisions"] == 0


def test_short_section_is_one_chunk_with_required_prefix():
    section = _node("section", "19A", "Penalty for false statements",
                     "19A. Penalty for false statements.— Whoever knowingly makes a false "
                     "statement shall be punished.", ordinal=1)
    result = build_chunks(TITLE, [section])

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.sequence_in_root == 1 and chunk.root_total_chunks == 1
    assert chunk.provision_ids == (section.id,)
    assert chunk.prefix == "The Example Act, 2026 — Section 19A — Penalty for false statements"
    assert chunk.chunk_text == chunk.prefix + "\n\n" + section.own_text
    assert chunk.chunk_text_sha256 and len(chunk.chunk_text_sha256) == 64
    assert chunk.reference_path == section.reference_path
    assert chunk.chunk_key == section.reference_path  # unsplit root keeps its own path


def test_prefix_format_matches_required_act_label_heading_pattern():
    section = _node("section", "7", "Short title", "7. Short title.", ordinal=1)
    assert build_prefix(TITLE, section) == "The Example Act, 2026 — Section 7 — Short title"


def test_prefix_without_heading_omits_trailing_segment():
    section = _node("section", "7", None, "7. Something.", ordinal=1)
    assert build_prefix(TITLE, section) == "The Example Act, 2026 — Section 7"


def test_prefix_for_rule_article_schedule_and_preamble():
    rule = _node("rule", "5", "Form of application", "5. Form.", ordinal=1)
    article = _node("article", "3", "Fundamental rights", "3. Rights.", ordinal=1)
    schedule = _node("schedule", "First", "Forms", "FIRST SCHEDULE", ordinal=1)
    preamble = _node("preamble", None, "Preamble", "WHEREAS it is expedient...", ordinal=1)

    assert build_prefix(TITLE, rule) == "The Example Act, 2026 — Rule 5 — Form of application"
    assert build_prefix(TITLE, article) == "The Example Act, 2026 — Article 3 — Fundamental rights"
    assert build_prefix(TITLE, schedule) == "The Example Act, 2026 — Schedule First — Forms"
    assert build_prefix(TITLE, preamble) == "The Example Act, 2026 — Preamble"


# ------------------------------------------------------------- splitting long sections

def _long_section_with_subsections(n_subsections: int, body_len: int = 40):
    section = _node("section", "10", "Offences and penalties",
                     "10. Offences and penalties.—", ordinal=1)
    nodes = [section]
    for i in range(1, n_subsections + 1):
        nodes.append(_node("subsection", str(i), None, f"({i}) " + "x" * body_len,
                           ordinal=1 + i, parent=section))
    return section, nodes


def test_long_section_splits_at_subsection_boundaries_not_mid_text():
    section, nodes = _long_section_with_subsections(n_subsections=8, body_len=40)
    # Each subsection body is ~44 chars; a budget of 100 forces several splits
    # while the section's own opening line always starts chunk 1.
    result = build_chunks(TITLE, nodes, ChunkConfig(max_chars=100))

    assert len(result.chunks) > 1
    all_root_ids = {chunk.root_provision_id for chunk in result.chunks}
    assert all_root_ids == {section.id}
    total = result.chunks[0].root_total_chunks
    assert total == len(result.chunks)
    assert [chunk.sequence_in_root for chunk in result.chunks] == list(range(1, total + 1))

    # Every chunk after the first is explicitly marked as a continuation.
    for chunk in result.chunks[1:]:
        assert f"(part {chunk.sequence_in_root} of {total})" in chunk.prefix
        assert chunk.chunk_key == f"{section.reference_path}/chunk:{chunk.sequence_in_root}"

    # No provision's own text is ever split across two chunks: every
    # subsection id appears in exactly one chunk, and the section itself
    # (its own leading text) opens chunk 1.
    seen = []
    for chunk in result.chunks:
        seen.extend(chunk.provision_ids)
    assert seen == [node.id for node in nodes]  # exact document order, no gaps/dupes
    assert section.id in result.chunks[0].provision_ids

    # Reassembling every chunk's text reproduces the full concatenation with
    # no alteration of the underlying provision text.
    for chunk in result.chunks:
        for node in nodes:
            if node.id in chunk.provision_ids:
                assert node.own_text.strip() in chunk.chunk_text


def test_short_section_with_subsections_is_not_split():
    section, nodes = _long_section_with_subsections(n_subsections=3, body_len=10)
    result = build_chunks(TITLE, nodes, ChunkConfig(max_chars=4000))
    assert len(result.chunks) == 1
    assert result.chunks[0].provision_ids == tuple(node.id for node in nodes)


def test_oversized_single_provision_is_kept_whole_with_warning():
    section = _node("section", "1", "Definitions", "1. Definitions.—" + ("z" * 500), ordinal=1)
    result = build_chunks(TITLE, [section], ChunkConfig(max_chars=50))
    assert len(result.chunks) == 1
    assert result.chunks[0].char_count > 50
    codes = {warning["code"] for warning in result.report["warnings"]}
    assert "oversized_single_provision" in codes


# ------------------------------------------------------------------- exclusions

def test_other_kind_is_never_chunked_and_never_included():
    section = _node("section", "1", "Title", "1. Title.", ordinal=1)
    note = _node("other", "3", "Source note 3", "3. Inserted by the 2019 Amendment Act.",
                 ordinal=2)
    result = build_chunks(TITLE, [section, note])
    assert len(result.chunks) == 1
    assert note.id not in result.chunks[0].provision_ids
    assert result.report["excluded_provisions"] == 1


@pytest.mark.parametrize("status", ["omitted", "repealed", "deleted"])
def test_explicit_source_status_excludes_node_and_descendants(status):
    section = _node("section", "5", "Repealed section", "5. [Repealed].", ordinal=1,
                     source_status=status)
    subsection = _node("subsection", "1", None, "(1) Some surviving-looking text.",
                       ordinal=2, parent=section)
    result = build_chunks(TITLE, [section, subsection])
    # The whole root is excluded, so neither it nor its child produces a chunk.
    assert result.chunks == []
    assert result.report["excluded_provisions"] == 2


def test_non_excluded_sibling_section_is_still_chunked_when_another_is_repealed():
    repealed = _node("section", "5", "Repealed", "5. [Repealed].", ordinal=1,
                     source_status="repealed")
    active = _node("section", "6", "Still in force", "6. Still in force.", ordinal=2)
    result = build_chunks(TITLE, [repealed, active])
    assert len(result.chunks) == 1
    assert result.chunks[0].root_provision_id == active.id


def test_part_and_chapter_containers_are_never_chunked_themselves():
    part = _node("part", "I", "Preliminary", "PART I — PRELIMINARY", ordinal=1)
    section = _node("section", "1", "Short title", "1. Short title.", ordinal=2, parent=part)
    result = build_chunks(TITLE, [part, section])
    assert len(result.chunks) == 1
    assert result.chunks[0].root_provision_id == section.id
    assert part.id not in result.chunks[0].provision_ids


# --------------------------------------------------------------- determinism / reruns

def test_build_is_deterministic_across_reruns():
    section, nodes = _long_section_with_subsections(n_subsections=6, body_len=30)
    config = ChunkConfig(max_chars=90)
    first = build_chunks(TITLE, nodes, config)
    second = build_chunks(TITLE, nodes, config)

    assert len(first.chunks) == len(second.chunks)
    for a, b in zip(first.chunks, second.chunks):
        assert a.chunk_text == b.chunk_text
        assert a.chunk_text_sha256 == b.chunk_text_sha256
        assert a.chunk_key == b.chunk_key
        assert a.provision_ids == b.provision_ids
    assert first.report == second.report


def test_config_sha256_changes_when_tunable_changes():
    assert ChunkConfig(max_chars=4000).config_sha256 != ChunkConfig(max_chars=2000).config_sha256
    assert ChunkConfig(max_chars=4000).config_sha256 == ChunkConfig(max_chars=4000).config_sha256


# ------------------------------------------------------------- multiple roots / ordering

def test_multiple_sections_are_ordered_and_individually_addressable():
    s1 = _node("section", "1", "First", "1. First.", ordinal=1)
    s2 = _node("section", "2", "Second", "2. Second.", ordinal=2)
    result = build_chunks(TITLE, [s2, s1])  # intentionally out of order input
    assert [chunk.ordinal for chunk in result.chunks] == [1, 2]
    assert result.chunks[0].root_provision_id == s1.id
    assert result.chunks[1].root_provision_id == s2.id


def test_schedule_splits_at_its_own_children_like_a_section():
    schedule = _node("schedule", "First", "Forms", "FIRST SCHEDULE", ordinal=1)
    rows = [_node("paragraph", str(i), None, f"Form {i}: " + ("y" * 40), ordinal=1 + i,
                 parent=schedule) for i in range(1, 5)]
    result = build_chunks(TITLE, [schedule, *rows], ChunkConfig(max_chars=80))
    assert len(result.chunks) > 1
    assert all(chunk.kind == "schedule" for chunk in result.chunks)
