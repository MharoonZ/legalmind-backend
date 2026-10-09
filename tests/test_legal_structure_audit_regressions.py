"""Regression fixtures for the October 2026 source-PDF structure audit."""

from uuid import uuid4

from app.services.legal_structure import (
    SourceBlock,
    SourceDocument,
    SourcePage,
    parse_structure,
)


def source(*pages: list[tuple[int, str]]) -> SourceDocument:
    file_id, extraction_id = uuid4(), uuid4()
    result_pages = []
    for page_index, contents in enumerate(pages, 1):
        page_id = uuid4()
        blocks = tuple(
            SourceBlock(
                uuid4(), page_id, extraction_id, file_id, page_index, ordinal,
                "paragraph", text,
                {"x0": 72, "y0": y, "x1": 520, "y1": y + 25}, 842,
            )
            for ordinal, (y, text) in enumerate(contents, 1)
        )
        result_pages.append(SourcePage(page_id, extraction_id, file_id, page_index,
                                       "extracted", [], 842, blocks))
    return SourceDocument(file_id, extraction_id, "The Example Act", "act",
                          len(result_pages), tuple(result_pages))


def node(parsed, suffix: str):
    matches = [item for item in parsed.nodes if item.reference_path.endswith(suffix)]
    assert len(matches) == 1, (suffix, [item.reference_path for item in matches])
    return matches[0]


def assert_exact(parsed, document):
    nodes = {item.id: item for item in parsed.nodes}
    blocks = {block.id: block for page in document.pages for block in page.blocks}
    for span in parsed.spans:
        assert nodes[span.provision_id].own_text[
            span.provision_char_start:span.provision_char_end
        ] == blocks[span.block_id].text[span.block_char_start:span.block_char_end]
    assert parsed.coverage["partial_blocks"] == parsed.coverage["unmapped_blocks"] == 0


def test_first_subsection_after_multiline_heading_and_amendment_marker():
    document = source([
        (140, "7. Powers of police officer to arrest and search persons and seize things liable to\n"
              "confiscation.\ufffd (1) The officer may arrest a person.\n"
              "(2) The person shall be produced before a court.\n"),
        (300, "1[11-B. Prohibition of keeping, carrying, or displaying arms.\ufffd 2[(1) "
              "The Secretary may prohibit display.]\n(2) An order may specify a place.\n"),
        (450, "28. \nRules.\ufffd (1) Government may make rules.\n(2) Rules may provide for records.\n"),
        (590, "32. Places for confinement.\u2013 1[(1)] Government may appoint places.\n"
              "6[(2) An officer may order removal.]\n"),
    ])
    parsed = parse_structure([document])
    for section in ("7", "11-B", "28", "32"):
        parent = node(parsed, f"/s:{section}")
        first = node(parsed, f"/s:{section}/sub:1")
        second = node(parsed, f"/s:{section}/sub:2")
        assert first.parent_id == second.parent_id == parent.id
        assert "(1)" not in parent.own_text
    assert node(parsed, "/s:11-B/sub:1").own_text.startswith("2[(1)")
    assert node(parsed, "/s:32/sub:1").own_text.startswith("1[(1)]")
    assert_exact(parsed, document)


def test_line_wrapped_cross_references_do_not_create_children():
    document = source([
        (140, "12. Cancellation.\ufffd (1) A licence may be suspended.\n"
              "(2) An appeal against the order under sub-section\n"
              "(I) may be made to the superior authority.\n"),
        (300, "13. Offences.\ufffd (1) Punishment applies under subsection\n"
              "(2) if a person commits an offence.\n"
              "(a) the first offence;\n6[(2) The offences are non-bailable.]\n"),
        (450, "36. Whipping.\ufffd Notwithstanding the provisions of clause\n"
              "(2) of section 35 no woman shall be whipped.\n"),
    ])
    parsed = parse_structure([document])
    assert not [item for item in parsed.nodes if item.reference_path.endswith("/s:12/sub:2/p:I")]
    assert [item.label for item in parsed.nodes if item.kind == "subsection"
            and item.parent_id == node(parsed, "/s:13").id] == ["1", "2"]
    assert not [item for item in parsed.nodes if item.kind == "subsection"
                and item.parent_id == node(parsed, "/s:36").id]
    assert "(I) may be made" in node(parsed, "/s:12/sub:2").own_text
    assert "(2) of section 35" in node(parsed, "/s:36").own_text
    assert_exact(parsed, document)


def test_historical_quote_and_notes_stay_out_of_operative_text():
    quotation = "Former wording of the provision, which is not current law. " * 4
    document = source([
        (160, "12. Cancellation.\ufffd (1) The authority may cancel a licence.\n"
              "(2) An appeal under sub-section\n(I) may be made within sixty days.\n"),
        (579, f"\u201c{quotation}\u201d\n1 Added by an amending Act.\n"),
        (650, "2 Substituted by a later Act.\n3 Omitted by another Act.\n"),
    ])
    parsed = parse_structure([document])
    operative = [item for item in parsed.nodes if item.kind != "other"]
    assert all("Former wording" not in item.own_text for item in operative)
    assert any(item.heading == "Unnumbered source note" and "Former wording" in item.own_text
               for item in parsed.nodes)
    assert {item.label for item in parsed.nodes if item.heading and
            item.heading.startswith("Source note ")} == {"1", "2", "3"}
    assert_exact(parsed, document)


def test_mixed_running_header_is_separate_from_cross_page_body():
    document = source(
        [(160, "37. Sick prisoners.\ufffd (1) The officer shall report the sick prisoner.\n")],
        [(69, "12\nThe Punjab Laws\n\n(2) The Jailer shall seek medical attention.\n"
              "38. Medical directions.\ufffd The directions shall be recorded.\n")],
    )
    parsed = parse_structure([document])
    assert [item.label for item in parsed.nodes if item.kind == "subsection"] == ["1", "2"]
    assert all("The Punjab Laws" not in item.own_text for item in parsed.nodes
               if item.kind != "other")
    assert any(item.heading == "Running header" and "The Punjab Laws" in item.own_text
               for item in parsed.nodes)
    assert_exact(parsed, document)


def test_nested_letter_lists_explanation_and_proviso_have_distinct_scope():
    document = source([
        (120, "26. Rules.\ufffd (1) Government may make rules to\n"
              "(i) prescribe prohibited articles; and\n"
              "(ii) regulate\ufffd\n(a) the management of schools;\n"
              "(b) the training of children;\n(c) visits;\n(d) education;\n"
              "(e) articles;\n(f) removal;\n(g) supply;\n(h) possession;\n"
              "(i) penalties for misconduct.\n"
              "(2) Government may publish rules.\n"),
        (320, "27. Definitions.\ufffd\n(c) consumer means a person who\ufffd\n"
              "(i) buys goods;\n(ii) hires services.\n"
              "3[Explanation: for the purpose of paragraph (c):\n"
              "(a) buying includes leasing;\n(b) commercial purpose is excluded.\n"
              "(d) damage means loss.\n(h) manufacturer includes a person who\ufffd\n"
              "(i) makes goods;\n(ii) labels goods;\n"
              "(i) manufacturing means producing goods.\n"),
        (600, "28. Penalty.\ufffd Whoever commits an offence\ufffd\n"
              "(a) knowingly carries arms; or\n(b) conceals arms,\n"
              "Provided that for a prohibited arm\ufffd\n"
              "(a) a heavy weapon; or\n(b) a grenade\n"
              "shall be punished more severely.\n"),
    ])
    parsed = parse_structure([document])
    assert node(parsed, "/s:26/sub:1/p:ii/p:i").own_text.startswith("(i) penalties")
    explanation = next(item for item in parsed.nodes
                       if item.heading == "Explanation" and
                       item.parent_id == node(parsed, "/s:27/p:c").id)
    assert [item.label for item in parsed.nodes if item.parent_id == explanation.id] == ["a", "b"]
    assert node(parsed, "/s:27/p:i").own_text.startswith("(i) manufacturing")
    proviso = next(item for item in parsed.nodes
                   if item.heading == "Proviso" and item.parent_id == node(parsed, "/s:28").id)
    assert [item.label for item in parsed.nodes if item.parent_id == proviso.id] == ["a", "b"]
    assert "shall be punished more severely" in proviso.own_text
    assert not [item for item in parsed.nodes if "/duplicate:" in item.reference_path]
    assert_exact(parsed, document)


def test_second_schedule_last_cell_is_not_moved_to_first_schedule():
    # The audit's A6 attribution was incorrect: this is a Second Schedule cell.
    document = source([
        (120, "THE FIRST SCHEDULE\nName of enactment | Punishment\nAct A | Two years\n"),
        (350, "THE SECOND SCHEDULE\n(See Section 29)\n"
              "Year | Extent of repeal\n1959 |"),
        (600, "Entry 8 of the first Schedule.\n"),
    ])
    parsed = parse_structure([document])
    assert "Entry 8 of the first Schedule" in node(parsed, "/sch:SECOND").own_text
    assert "Entry 8 of the first Schedule" not in node(parsed, "/sch:FIRST").own_text
    assert_exact(parsed, document)
