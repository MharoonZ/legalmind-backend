"""Persisted-data diagnostics must expose structural gaps without declaring legal correctness."""

from types import SimpleNamespace as Row

from app.services.legal_audit import source_page_review_findings, structure_findings


def node(identity: str, label: str, text: str = "") -> Row:
    return Row(id=identity, kind="section", label=label,
               own_text=text, reference_path=f"s:{label}")


def block(text: str, kind: str = "paragraph") -> Row:
    return Row(block_kind=kind, normalized_text=text)


def span(identity: str, start: int, end: int) -> Row:
    return Row(provision_id=identity, provision_char_start=start,
               provision_char_end=end, block_char_start=start,
               block_char_end=end, mapping_quality="exact")


def test_reports_missing_saved_sections_and_duplicate_labels():
    nodes = [node(str(number), str(number)) for number in (9, 11, 18, 21, 21)]
    findings = structure_findings(nodes, [])
    messages = [item["message"] for item in findings]
    assert any("number 10" in message for message in messages)
    assert any("number 19–20" in message for message in messages)
    assert any("occurs 2 times" in message for message in messages)


def test_checks_saved_provision_text_against_exact_block_offsets():
    nodes = [node("a", "1", "Section 1 text"), node("b", "2", "Missing"),
             node("c", "3", "Wrong")]
    rows = [(span("a", 0, 14), Row(normalized_text="Section 1 text")),
            (span("c", 0, 5), Row(normalized_text="Other"))]
    findings = structure_findings(nodes, rows)
    codes_by_id = {(item["provision_id"], item["code"]) for item in findings}
    assert ("a", "span_text_mismatch") not in codes_by_id
    assert ("a", "uncovered_provision_text") not in codes_by_id
    assert ("b", "missing_source_span") in codes_by_id
    assert ("c", "span_text_mismatch") in codes_by_id
    assert ("c", "uncovered_provision_text") in codes_by_id


def test_whitespace_between_spans_is_not_a_false_gap():
    nodes = [node("a", "1", "One\nTwo")]
    rows = [(span("a", 0, 3), Row(normalized_text="One")),
            (Row(provision_id="a", provision_char_start=4, provision_char_end=7,
                 block_char_start=0, block_char_end=3, mapping_quality="exact"),
             Row(normalized_text="Two"))]
    assert structure_findings(nodes, rows) == []


def test_audit_flags_explicit_and_linked_nonoperative_status_missing():
    direct = Row(id="s25", kind="section", label="25",
                 own_text="25. [Former text]. Deleted by the Amendment Act.",
                 source_status=None, parent_id=None, reference_path="s:25")
    child = Row(id="sub3", kind="subsection", label="3",
                own_text="3[(3) * * *]", source_status=None,
                parent_id="s25", reference_path="s:25/sub:3")
    note = Row(id="note", kind="other", label="3",
               own_text="3 Sub-section (3) omitted by the Amendment Act.",
               source_status=None, parent_id=None, reference_path="note:3")
    findings = structure_findings(
        [direct, child, note], [],
        note_links=[Row(note_provision_id="note", target_provision_id="sub3")],
    )
    assert {item["code"] for item in findings} >= {
        "explicit_source_status_missing", "linked_note_source_status_missing",
    }


def test_critical_finding_when_numbered_act_body_is_saved_as_contents():
    headings = "\n".join(f"{number}. Section heading {number}" for number in range(1, 33))
    contents = Row(id="contents", kind="other", label=None,
                   heading="Contents continuation", own_text=headings,
                   reference_path="other:contents")
    findings = structure_findings(
        [contents], [], source_blocks=[block("CONTENTS\n" + headings), block(headings)],
        instrument_type="act",
    )
    codes = {(item["code"], item["severity"]) for item in findings}
    assert ("no_operative_structure", "error") in codes
    assert ("contents_mode_into_body", "error") in codes
    assert any("32" in item["message"] for item in findings)


def test_critical_finding_for_implausibly_few_saved_sections():
    source = [block("\n".join(f"{number}. Text" for number in range(1, 11)))]
    findings = structure_findings([node("section-1", "1")], [],
                                  source_blocks=source, instrument_type="act")
    assert ("too_few_operative_sections", "error") in {
        (item["code"], item["severity"]) for item in findings
    }


def test_complete_structure_and_source_notes_do_not_trigger_critical_gate():
    sections = [node(str(number), str(number)) for number in range(1, 5)]
    source = [block("\n".join(f"{number}. Text" for number in range(1, 5))),
              block("1. Quoted previous law\n2. Historical note\n3. Amendment", "footnote")]
    findings = structure_findings(sections, [], source_blocks=source, instrument_type="act")
    assert not any(item["severity"] == "error" for item in findings)


def test_rules_compare_rule_nodes_not_section_nodes():
    source = [block("Rule 1. One\nRule 2. Two\nRule 3. Three")]
    saved = [Row(id="r1", kind="rule", label="1", own_text="", reference_path="r:1")]
    findings = structure_findings(saved, [], source_blocks=source, instrument_type="rule")
    assert not any(item["code"] == "no_operative_structure" for item in findings)


def duplicated_contents_fixture(*, labels=range(1, 7), contents_marker=True,
                                same_extraction=True):
    nodes = []
    spans = []
    blocks = []
    if contents_marker:
        blocks.append(Row(extraction_id="pdf-1", page_id="front", page_index=1,
                          block_kind="heading", normalized_text="C O N T E N T S"))
    for label in labels:
        for side, page, extraction in (
            ("front", 1, "pdf-1"),
            ("body", 3, "pdf-1" if same_extraction else "pdf-2"),
        ):
            identity = f"{side}-{label}"
            value = (f"{label}. Short heading" if side == "front" else
                     f"{label}. Full operative section with substantially longer legal text "
                     "that is not a table-of-contents entry.")
            nodes.append(Row(id=identity, kind="section", label=str(label),
                             own_text=value, reference_path=f"s:{label}/{side}"))
            block_row = Row(extraction_id=extraction, page_id=side, page_index=page,
                            block_kind="paragraph", normalized_text=value)
            blocks.append(block_row)
            spans.append((Row(provision_id=identity, extraction_id=extraction,
                              page_id=side, provision_char_start=0,
                              provision_char_end=len(value), block_char_start=0,
                              block_char_end=len(value), mapping_quality="exact"),
                          block_row))
    return nodes, spans, blocks


def test_many_contents_entries_saved_as_sections_are_critical():
    nodes, spans, blocks = duplicated_contents_fixture()
    findings = structure_findings(nodes, spans, source_blocks=blocks,
                                  instrument_type="act")
    assert ("contents_entries_as_provisions", "error") in {
        (item["code"], item["severity"]) for item in findings
    }


def test_small_or_unproven_duplicate_cohort_is_not_critical():
    for options in ({"labels": range(1, 5)}, {"contents_marker": False},
                    {"same_extraction": False}):
        nodes, spans, blocks = duplicated_contents_fixture(**options)
        findings = structure_findings(nodes, spans, source_blocks=blocks,
                                      instrument_type="act")
        assert not any(item["code"] == "contents_entries_as_provisions"
                       for item in findings)


def test_source_audit_blocks_tables_and_glyph_ambiguity_but_honors_blank_review():
    pages = [
        ("blank", "act.pdf", 2, "needs_review", [{"code": "blank_candidate"}], []),
        ("table", "act.pdf", 11, "needs_review", [{"code": "table_layout_candidate"}], [{}]),
        ("glyph", "act.pdf", 3, "needs_review", [{"code": "ambiguous_numbering_glyph"}], []),
    ]
    initial = source_page_review_findings(pages, {})
    assert [(item["code"], item["severity"]) for item in initial] == [
        ("source_page_needs_review", "error"),
        ("table_cells_need_review", "error"),
        ("source_page_needs_review", "error"),
    ]
    reviewed = source_page_review_findings(pages, {"blank": Row(decision="mark_blank")})
    assert len(reviewed) == 2
    assert any("page 11" in item["message"] for item in reviewed)
    assert any("page 3" in item["message"] for item in reviewed)
    rows = [
        [{"text": "Year"}, {"text": "No."}, {"text": "Title"}, {"text": "Extent"}],
        [{"text": "1"}, {"text": "2"}, {"text": "3"}, {"text": "4"}],
        [{"text": "1959"}, {"text": "IV"}, {"text": "Act"}, {"text": "The whole."}],
    ]
    tables_reviewed = [pages[0],
                       ("table", "act.pdf", 11, "extracted", [], [{
                           "status": "visually_reviewed_rows", "reviewed_by": "Visual reviewer",
                           "review_note": "Compared with PDF", "column_count": 4, "rows": rows,
                           "atomic_rows": [
                               {"source_row_index": index,
                                "cells": [cell["text"] for cell in row]}
                               for index, row in enumerate(rows)
                           ],
                       }])]
    assert source_page_review_findings(tables_reviewed, {"blank": Row(decision="mark_blank")}) == []


def test_short_contents_then_body_cannot_pass_with_zero_sections():
    saved = [Row(id="contents", parent_id=None, kind="other", label=None,
                 heading="Contents continuation", reference_path="other:contents",
                 own_text="THE EXAMPLE ACT\nPreamble. WHEREAS needed.\n"
                          "1. Short title. Full text.\n2. Application. Full text.")]
    findings = structure_findings(
        saved, [], source_blocks=[block("CONTENTS\n1. Short title\n2. Application"),
                                  block(saved[0].own_text)], instrument_type="act",
    )
    codes = {item["code"] for item in findings}
    assert {"no_operative_structure", "contents_mode_into_body"} <= codes


def test_auditor_detects_missing_first_child_false_headings_and_wrong_parents():
    rows = [
        Row(id="s1", parent_id=None, kind="section", label="1", reference_path="s:1",
            own_text="1. Short title. (1)This Act applies."),
        Row(id="sub2", parent_id="s1", kind="subsection", label="2",
            reference_path="s:1/sub:2", own_text="(2) Later text."),
        Row(id="s6", parent_id=None, kind="section", label="6", reference_path="s:6",
            own_text="6. Real section. Body."),
        Row(id="fake6", parent_id=None, kind="section", label="6", reference_path="s:6/duplicate:2",
            own_text="section 6. "),
        Row(id="fakechapter", parent_id=None, kind="chapter", label="XVI",
            heading="of the Pakistan Penal Code", reference_path="ch:XVI",
            own_text="Chapter XVI of the Pakistan Penal Code"),
        Row(id="s21", parent_id="fakechapter", kind="section", label="21",
            reference_path="ch:XVI/s:21", own_text="21. Later section."),
        Row(id="suborphan", parent_id="fakechapter", kind="subsection", label="3",
            reference_path="ch:XVI/sub:3", own_text="(3) Orphan."),
    ]
    codes = {item["code"] for item in structure_findings(rows, [])}
    assert {"missing_first_subsection", "inline_reference_as_section",
            "sentence_reference_as_division", "late_division_reparenting",
            "invalid_subsection_parent"} <= codes


def test_auditor_detects_list_continuation_and_proviso_hierarchy():
    rows = [
        Row(id="s", parent_id=None, kind="section", label="8", reference_path="s:8", own_text="8. Procedure."),
        Row(id="sub", parent_id="s", kind="subsection", label="1", reference_path="s:8/sub:1",
            own_text="(1) The officer may"),
        Row(id="a", parent_id="sub", kind="paragraph", label="a", reference_path="s:8/sub:1/p:a",
            own_text="(a) inspect;"),
        Row(id="c", parent_id="a", kind="paragraph", label="b", reference_path="s:8/sub:1/p:a/p:b",
            own_text="(b) seize documents,\nshall be punishable."),
        Row(id="proviso", parent_id="sub", kind="other", label=None, heading="Proviso",
            reference_path="s:8/sub:1/other:1", own_text="Provided that a warrant is required."),
        Row(id="next", parent_id="proviso", kind="paragraph", label="b",
            reference_path="s:8/sub:1/other:1/p:b", own_text="(b) demand records."),
    ]
    codes = {item["code"] for item in structure_findings(rows, [])}
    assert {"sequential_sibling_nested", "post_list_continuation_in_child",
            "sibling_beneath_proviso"} <= codes
