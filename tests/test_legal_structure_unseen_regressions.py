"""Generic fixtures distilled from unseen Acts, without document-specific rules."""

from types import SimpleNamespace as Row
from uuid import uuid4

from app.services.legal_audit import structure_findings
from app.services.legal_structure import SourceBlock, SourceDocument, SourcePage, parse_structure


def source(*pages: list[tuple[int, str] | tuple[int, str, str]]) -> SourceDocument:
    file_id, extraction_id = uuid4(), uuid4()
    built = []
    for page_index, items in enumerate(pages, 1):
        page_id = uuid4()
        blocks = tuple(SourceBlock(
            uuid4(), page_id, extraction_id, file_id, page_index, ordinal,
            item[2] if len(item) == 3 else "paragraph", item[1],
            {"x0": 72, "y0": item[0], "x1": 530, "y1": item[0] + 24}, 842,
        ) for ordinal, item in enumerate(items, 1))
        built.append(SourcePage(page_id, extraction_id, file_id, page_index,
                                "extracted", [], 842, blocks))
    return SourceDocument(file_id, extraction_id, "The Example Ordinance",
                          "ordinance", len(built), tuple(built))


def one(parsed, kind: str, label: str):
    matches = [node for node in parsed.nodes if node.kind == kind and node.label == label]
    assert len(matches) == 1, [(node.kind, node.label, node.reference_path)
                                for node in parsed.nodes]
    return matches[0]


def assert_source_exact(parsed, document):
    blocks = {block.id: block for page in document.pages for block in page.blocks}
    nodes = {node.id: node for node in parsed.nodes}
    for span in parsed.spans:
        assert nodes[span.provision_id].own_text[span.provision_char_start:span.provision_char_end] == (
            blocks[span.block_id].text[span.block_char_start:span.block_char_end]
        )
    assert parsed.coverage["partial_blocks"] == parsed.coverage["unmapped_blocks"] == 0


def test_short_run_in_caption_opens_next_subsection_across_pages():
    document = source(
        [(110, "6. Penalty.\u2013 (1) Whoever commits an offence shall be punished.\n")],
        [(110, "Presumption.\u2013 (2) Where a person is found there, a presumption applies.\n"),
         (230, "7. Next provision.\u2013 Other operative text.\n")],
    )
    parsed = parse_structure([document])
    parent = one(parsed, "section", "6")
    first, second = one(parsed, "subsection", "1"), one(parsed, "subsection", "2")
    assert first.parent_id == second.parent_id == parent.id
    assert second.own_text.startswith("Presumption.\u2013 (2)")
    assert "Presumption" not in first.own_text
    assert_source_exact(parsed, document)


def test_ordinary_sentence_with_parenthesized_number_is_not_a_new_subsection():
    document = source([(110, "6. Penalty.\u2013 (1) A person may rely on the following text.\n"
                           "Presumption is that (2) references are illustrative only.\n")])
    parsed = parse_structure([document])
    assert [node.label for node in parsed.nodes if node.kind == "subsection"] == ["1"]
    assert "Presumption is that (2)" in one(parsed, "subsection", "1").own_text
    assert_source_exact(parsed, document)


def test_inline_penalty_after_last_list_item_returns_to_parent():
    document = source([(110, "4. Offence.\u2013 Whoever commits either act\u2013\n"
                           "(a) solicits another; or\n"
                           "(b) assists another, shall for a first offence be punished with a fine.\n")])
    parsed = parse_structure([document])
    parent = one(parsed, "section", "4")
    assert "shall for a first offence be punished" in parent.own_text
    assert "shall for a first offence be punished" not in one(parsed, "paragraph", "b").own_text
    assert_source_exact(parsed, document)


def test_penalty_continuation_on_following_page_returns_to_parent():
    document = source(
        [(110, "4. Offence.\u2013 Where a person\u2013\n(a) solicits another; or\n"
               "(b) assists another,\n")],
        [(110, "shall be liable to imprisonment for six months.\n")],
    )
    parsed = parse_structure([document])
    assert "shall be liable" in one(parsed, "section", "4").own_text
    assert "shall be liable" not in one(parsed, "paragraph", "b").own_text
    assert_source_exact(parsed, document)


def test_shared_penalty_split_across_source_blocks_returns_to_parent():
    document = source([(110, "4. Offence.\u2013 Whoever commits either act\u2013\n"
                            "(a) solicits another; or\n"
                            "(b) assists another, shall for a first \n"),
                       (220, "offence be punished with a fine.\n")])
    parsed = parse_structure([document])
    assert "shall for a first" in one(parsed, "section", "4").own_text
    assert "offence be punished" in one(parsed, "section", "4").own_text
    assert "shall for a first" not in one(parsed, "paragraph", "b").own_text
    assert_source_exact(parsed, document)


def test_independent_last_child_penalty_stays_with_that_child():
    document = source([(110, "4. Separate penalties.\u2013 The following alternatives apply:\n"
                           "(a) a first offender pays a fine;\n"
                           "(b) a repeat offender shall be punished by imprisonment.\n")])
    parsed = parse_structure([document])
    assert "shall be punished" in one(parsed, "paragraph", "b").own_text
    assert "shall be punished" not in one(parsed, "section", "4").own_text
    assert_source_exact(parsed, document)


def test_isolated_source_markers_do_not_create_sections_or_shift_subsections():
    document = source([(110, "1. Short title.\u2013 (1) This is the title.\n"
                            "2. Definitions.\u2013 Definitions apply.\n"
                            "3. Powers.\u2013 (1) This power continues under the Ordinance, 1948\n"),
                       (280, "1. \n(2) Second power applies.\n(3) Third power applies.\n"
                             "The Code of 1898\n"),
                       (430, "7. \n(4) Fourth power applies.\n"
                             "4. Next section.\u2013 It follows.\n"
                             "7. Genuine later section.\u2013 It is operative.\n"),
                       (690, "1VIII of 1948.\n7VII of 1898.\n")])
    parsed = parse_structure([document])
    assert [node.label for node in parsed.nodes if node.kind == "section"] == ["1", "2", "3", "4", "7"]
    section = one(parsed, "section", "3")
    assert [node.label for node in parsed.nodes
            if node.kind == "subsection" and node.parent_id == section.id] == ["1", "2", "3", "4"]
    assert [node.label for node in parsed.nodes if node.kind == "section"].count("7") == 1
    assert_source_exact(parsed, document)


def test_substantive_genuine_sections_one_and_seven_remain_headings():
    document = source([(110, "1. Short title.\u2013 The Ordinance has a title.\n"
                           "7. Offences.\u2013 A person shall comply.\n")])
    parsed = parse_structure([document])
    assert [node.label for node in parsed.nodes if node.kind == "section"] == ["1", "7"]
    assert_source_exact(parsed, document)


def test_schedule_lettered_entries_get_child_nodes_and_exact_spans():
    document = source([(110, "3. Application.\u2013 The Schedule applies.\n"
                           "SCHEDULE\n[SECTION 3]\n"
                           "(a) First offence;\n(b) Second offence; and\n(c) Third offence.\n")])
    parsed = parse_structure([document])
    schedule = next(node for node in parsed.nodes if node.kind == "schedule")
    assert [node.label for node in parsed.nodes
            if node.kind == "paragraph" and node.parent_id == schedule.id] == ["a", "b", "c"]
    assert "(a)" not in schedule.own_text
    assert_source_exact(parsed, document)


def test_schedule_parenthesized_prose_and_table_are_not_paragraphs():
    document = source([(110, "SCHEDULE\nThe phrase (a) is mentioned only in running prose.\n"),
                       (260, "(a) table cell\n(b) table cell\n", "table")])
    parsed = parse_structure([document])
    assert [node for node in parsed.nodes if node.kind == "paragraph"] == []
    assert_source_exact(parsed, document)


def test_auditor_flags_deliberately_corrupted_saved_tree():
    nodes = [
        Row(id="s1", parent_id=None, kind="section", label="1", heading="Short title",
            reference_path="s:1", own_text="1. Short title."),
        Row(id="s6", parent_id=None, kind="section", label="6", heading="Penalty",
            reference_path="s:6", own_text="6. Penalty.– Whoever commits either act–"),
        Row(id="sub1", parent_id="s6", kind="subsection", label="1", heading=None,
            reference_path="s:6/sub:1", own_text="(1) First rule.\nPresumption.\u2013 (2) Second rule."),
        Row(id="a", parent_id="s6", kind="paragraph", label="a", heading=None,
            reference_path="s:6/p:a", own_text="(a) First alternative; or"),
        Row(id="b", parent_id="s6", kind="paragraph", label="b", heading=None,
            reference_path="s:6/p:b", own_text="(b) Second alternative, shall for a first offence be punished."),
        Row(id="fake1", parent_id=None, kind="section", label="1", heading=None,
            reference_path="s:1/duplicate:2", own_text="1. "),
        Row(id="wrongsub", parent_id="fake1", kind="subsection", label="3", heading=None,
            reference_path="s:1/duplicate:2/sub:3", own_text="(3) Misparented."),
        Row(id="schedule", parent_id=None, kind="schedule", label=None, heading=None,
            reference_path="sch:1", own_text="SCHEDULE\n(a) First;\n(b) Second;\n(c) Third."),
    ]
    codes = {item["code"] for item in structure_findings(nodes, [])}
    assert {"embedded_subsection_missing", "post_list_continuation_in_child",
            "isolated_marker_as_section", "weak_heading_reparenting",
            "schedule_items_unstructured"} <= codes
