"""Persisted-data diagnostics must expose structural gaps without declaring legal correctness."""

from types import SimpleNamespace as Row

from app.services.legal_audit import structure_findings


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
