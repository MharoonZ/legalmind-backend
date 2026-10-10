"""Printed layout patterns from the six pilot Acts, without external PDF fixtures.

These intentionally preserve amendment glyphs, page breaks, and block offsets.
"""

from uuid import uuid4

from app.services.legal_structure import (
    SourceBlock, SourceDocument, SourcePage, parse_structure,
)


def document(title: str, pages: list[list[tuple[int, str]]]) -> SourceDocument:
    file_id, extraction_id = uuid4(), uuid4()
    source_pages = []
    for page_index, lines in enumerate(pages, 1):
        page_id = uuid4()
        blocks = tuple(SourceBlock(
            id=uuid4(), page_id=page_id, extraction_id=extraction_id,
            file_id=file_id, page_index=page_index, ordinal=index,
            kind="paragraph", text=text,
            bbox={"x0": 40, "y0": y, "x1": 540, "y1": y + 22},
            page_height=800,
        ) for index, (y, text) in enumerate(lines, 1))
        source_pages.append(SourcePage(page_id, extraction_id, file_id, page_index,
                                       "extracted", [], 800, blocks))
    return SourceDocument(file_id, extraction_id, title, "act", len(pages),
                          tuple(source_pages))


def node(parsed, suffix: str):
    matches = [item for item in parsed.nodes if item.reference_path.endswith(suffix)]
    assert len(matches) == 1, (suffix, [item.reference_path for item in matches])
    return matches[0]


def assert_exact(parsed, doc):
    nodes = {item.id: item for item in parsed.nodes}
    blocks = {block.id: block for page in doc.pages for block in page.blocks}
    for span in parsed.spans:
        assert nodes[span.provision_id].own_text[span.provision_char_start:span.provision_char_end] == (
            blocks[span.block_id].text[span.block_char_start:span.block_char_end]
        )
    assert parsed.coverage["partial_blocks"] == 0
    assert parsed.coverage["unmapped_blocks"] == 0


def test_multi_page_chapter_contents_stays_front_matter_until_body_title():
    doc = document("The Example Prisons Act, 1894", [
        [(70, "THE EXAMPLE PRISONS ACT, 1894\n"),
         (125, "C O N T E N T S\nSECTION HEADING\n"),
         (200, "CHAPTER I\nPRELIMINARY\n1. \nTitle and extent.\n")],
        [(70, "2\nThe Punjab Laws\n"),
         (150, "CHAPTER II\nOFFICERS\n2. \nDirectors of Prisons.\n")],
        [(70, "1THE EXAMPLE PRISONS ACT, 1894\n"),
         (150, "An Act to regulate prisons.\nWHEREAS it is expedient...\n"),
         (240, "CHAPTER I\nPRELIMINARY\n1. Title and extent.� The law begins.\n")],
        [(70, "4\nThe Punjab Laws\n"),
         (150, "CHAPTER II\nOFFICERS\n5[2. Directors of Prisons.� Body provision.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "chapter"] == ["I", "II"]
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["1", "2"]
    assert node(parsed, "/ch:II/s:2").own_text.startswith("5[2.")
    assert any(n.kind == "other" and "Directors of Prisons" in n.own_text
               and "Contents" in (n.heading or "") for n in parsed.nodes)
    assert_exact(parsed, doc)


def test_multi_page_part_contents_does_not_duplicate_parts_sections_or_schedules():
    doc = document("The Example Prisoners Act, 1900", [
        [(70, "THE EXAMPLE PRISONERS ACT, 1900\n"),
         (125, "C O N T E N T S\n"),
         (200, "PART I\nPRELIMINARY\n1. \nShort title.\nPART VI\nREMOVAL\n29. \nRemoval.\n")],
        [(70, "2\nThe Punjab Laws\n"),
         (130, "THE FIRST SCHEDULE\nTHE SECOND SCHEDULE\nTHE THIRD SCHEDULE\n")],
        [(70, "1THE EXAMPLE PRISONERS ACT, 1900\n"),
         (150, "An Act to consolidate the law relating to prisoners.\n"),
         (240, "PART VI\nREMOVAL\n1[29. Removal of prisoners.� (1) Operative text.\n")],
        [(160, "THE FIRST SCHEDULE\nForm 1 ______\n"),
         (300, "THE SECOND SCHEDULE\nForm 2 ______\n"),
         (450, "[THE THIRD SCHEDULE]. Repealed by the Repealing Act.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "part"] == ["VI"]
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["29"]
    assert [n.label for n in parsed.nodes if n.kind == "schedule"] == ["FIRST", "SECOND", "THIRD"]
    assert node(parsed, "/part:VI/s:29").own_text.startswith("1[29.")
    assert "______" in node(parsed, "/sch:SECOND").own_text
    assert_exact(parsed, doc)


def test_roman_dash_divisions_and_jointly_repealed_heading_keep_printed_text():
    doc = document("The Reformatory Schools Act, 1897", [
        [(70, "THE REFORMATORY SCHOOLS ACT, 1897\n"),
         (125, "C O N T E N T S\n"),
         (180, "I�PRELIMINARY\n1. \nTitle and extent.\n")],
        [(70, "2\nThe Punjab Laws\n"),
         (150, "II�REFORMATORY SCHOOLS\n5. \nPower to establish.\n")],
        [(70, "1THE REFORMATORY SCHOOLS ACT, 1897\n"),
         (150, "An Act to amend the law relating to schools.\n"),
         (230, "I�PRELIMINARY\n1. Title and extent.� (1) Short title.\n"
               "2 and 3. [Repeals]. Repealed by the Repealing Act.\n4. Definitions.� Defined terms.\n")],
        [(180, "II�REFORMATORY SCHOOLS\n5. Power to establish.� New text.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "part"] == ["I", "II"]
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["1", "2 and 3", "4", "5"]
    assert node(parsed, "/part:I/s:2 and 3").own_text.startswith("2 and 3. [Repeals]")
    assert node(parsed, "/part:I/s:1/sub:1").own_text.startswith("(1)")
    assert_exact(parsed, doc)


def test_inline_and_amendment_prefixed_first_subsections_are_children():
    doc = document("The Good Conduct Prisoners Act, 1926", [
        [(100, "1. Short title and extent.� (1) This Act may be called the Good Conduct Act.\n"
               "3[(2) It extends to the Punjab].\n(3) It shall commence later.\n"
               "6. Delegation.� (1) The Government may delegate.\n(2) Another clause.\n"
               "7. Repeal.� (1) An earlier law is repealed.\n(2) Savings.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["1", "6", "7"]
    for label in ("1", "6", "7"):
        assert node(parsed, f"/s:{label}/sub:1").parent_id == node(parsed, f"/s:{label}").id
    assert node(parsed, "/s:1/sub:2").own_text.startswith("3[(2)")
    assert_exact(parsed, doc)


def test_parenthesized_place_and_explanatory_word_remain_prose():
    doc = document("The Restriction of Habitual Offenders Act, 1918", [
        [(70, "1THE RESTRICTION OF HABITUAL OFFENDERS\n"),
         (100, "(PUNJAB) ACT, 1918\n"),
         (180, "1. Title and extent.� (a) This Act may be called the Restriction of Habitual "
               "Offenders (Punjab) Act, 1918.\n(b) It extends to the Punjab.\n"
               "17. Penalties.� (1) Whoever violates the order commits an offence.\n"
               "(2) The term is inclusive (inclusive) of earlier laws.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "paragraph"] == ["a", "b"]
    assert "(Punjab) Act" in node(parsed, "/s:1/p:a").own_text
    assert "(inclusive)" in node(parsed, "/s:17/sub:2").own_text
    assert node(parsed, "/s:17/sub:1").own_text.startswith("(1)")
    assert_exact(parsed, doc)


def test_footnote_part_heading_is_note_not_part_and_body_resumes_next_page():
    doc = document("The Punjab Borstal Act, 1926", [
        [(150, "32. Application of laws.� The Prisons Act, 1894 and Prisoners Act, 1900 apply\n"),
         (610, "1Substituted in Schedule I,\n"),
         (680, "Part II.\n2Added by another amendment.\n")],
        [(75, "2\nThe Punjab Laws\n"),
         (160, "to sections 35 to 50 (inclusive) of the Prisoners Act, 1900.\n"
               "33. Repeal.� Repeal text.\n")],
    ])
    parsed = parse_structure([doc])
    assert not [n for n in parsed.nodes if n.kind == "part"]
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["32", "33"]
    section32 = node(parsed, "/s:32")
    assert "(inclusive)" in section32.own_text
    assert {s.page_id for s in parsed.spans if s.provision_id == section32.id} == {
        doc.pages[0].id, doc.pages[1].id,
    }
    assert any(n.kind == "other" and "Part II." in n.own_text for n in parsed.nodes)
    assert_exact(parsed, doc)


def test_unbracketed_year_suffix_links_only_to_same_page_source_note():
    doc = document("The Punjab Vagrancy Ordinance, 1958", [
        [(150, "26. Repeal.� The Sind Vagrancy Act, 19472, is hereby repealed.\n"),
         (650, "2Sind XVII of 1947.\n")],
    ])
    parsed = parse_structure([doc])
    assert len(parsed.note_links) == 1
    link = parsed.note_links[0]
    assert link.marker_label == "2"
    assert link.target_provision_id == node(parsed, "/s:26").id
    block = doc.pages[0].blocks[0]
    assert block.text[link.block_char_start:link.block_char_end] == "2"
    assert block.text[link.block_char_start - 4:link.block_char_end] == "19472"
    assert_exact(parsed, doc)


def test_repealed_unlabelled_schedule_is_body_structure_not_a_section_tail():
    doc = document("The Prisons Act, 1894", [
        [(150, "62. Exercise of powers of Superintendent and Medical Officer.� Final text.\n"),
         (350, "THE SCHEDULE-[ENACTMENTS REPEALED].\n"
               "REPEALED BY THE REPEALING ACT, 1938.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["62"]
    schedules = [n for n in parsed.nodes if n.kind == "schedule"]
    assert len(schedules) == 1 and schedules[0].label is None
    assert schedules[0].own_text.startswith("THE SCHEDULE-")
    assert "THE SCHEDULE" not in node(parsed, "/s:62").own_text
    assert_exact(parsed, doc)


def test_title_case_schedule_period_is_continuation_not_unlabelled_heading():
    doc = document("The Reformatory Schools Act, 1897", [
        [(150, "2 and 3. [Repeals]. Repealed by the Repealing Act, section 2 and\n"
               "Schedule.\n4. Definitions.� New text.\n")],
    ])
    parsed = parse_structure([doc])
    assert not [n for n in parsed.nodes if n.kind == "schedule"]
    assert "Schedule." in node(parsed, "/s:2 and 3").own_text
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["2 and 3", "4"]
    assert_exact(parsed, doc)


def test_short_roman_footnotes_stay_separate_from_final_operating_section():
    doc = document("The Prevention of Corruption Ordinance, 1958", [
        [(150, "4. Repeal.� The Bahawalpur Prevention of Corruption Act, 1950, "
               "is hereby repealed.\n"),
         (640, "1This Ordinance extended Act II of 1947.\n"),
         (690, "2XLV of 1860.\n3V of 1898.\n")],
    ])
    parsed = parse_structure([doc])
    assert node(parsed, "/s:4").own_text.strip().endswith("is hereby repealed.")
    assert [n.label for n in parsed.nodes if n.heading and n.heading.startswith("Source note")] == [
        "1", "2", "3",
    ]
    assert_exact(parsed, doc)


def test_code_and_year_suffix_markers_link_to_matching_notes_with_exact_offsets():
    doc = document("The Prevention of Corruption Ordinance, 1958", [
        [(150, "3. Amendment.� The Pakistan Penal Code2 or the Code of Criminal Procedure, "
               "18983, shall apply.\n4. Repeal.� Another Act is hereby repealed.\n"),
         (650, "1This Ordinance was promulgated later.\n"),
         (690, "Its text was published in the Gazette.\n2XLV of 1860.\n3V of 1898.\n")],
    ])
    parsed = parse_structure([doc])
    links = sorted(parsed.note_links, key=lambda link: link.marker_label)
    assert [link.marker_label for link in links] == ["2", "3"]
    assert all(link.target_provision_id == node(parsed, "/s:3").id for link in links)
    block = doc.pages[0].blocks[0]
    assert [block.text[link.block_char_start:link.block_char_end] for link in links] == ["2", "3"]
    assert block.text[links[0].block_char_start - 4:links[0].block_char_end] == "Code2"
    assert block.text[links[1].block_char_start - 4:links[1].block_char_end] == "18983"
    assert_exact(parsed, doc)


def test_body_word_contents_does_not_restart_front_matter():
    doc = document("The Example Arms Act, 1965", [
        [(120, "1. Short title. The Act begins.\n")],
        [(120, "18. Confiscation. The contents of a package may be confiscated.\n"
               "19. Search. A magistrate may order a search.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["1", "18", "19"]
    assert not [n for n in parsed.nodes if (n.heading or "").startswith("Contents")]
    assert_exact(parsed, doc)


def test_hyphenated_amendment_sections_and_chapters_keep_exact_source():
    doc = document("The Example Act, 2005", [
        [(120, "CHAPTER-I\nPRELIMINARY\n1. Short title. Text.\n"
               "2[1-A. Added duty. (1) First clause.\n(2) Second clause.]\n"
               "CHAPTER-II\nOPERATIVE\n2. Another duty. Text.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "chapter"] == ["I", "II"]
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["1", "1-A", "2"]
    assert node(parsed, "/ch:I/s:1-A/sub:1").parent_id == node(parsed, "/ch:I/s:1-A").id
    assert node(parsed, "/ch:I/s:1-A").own_text.startswith("2[1-A.")
    assert_exact(parsed, doc)


def test_schedule_table_rows_are_not_operative_sections():
    doc = document("The Example Ordinance, 1959", [
        [(100, "29. Repeal. This repeals an older Act.\n"
               "THE FIRST SCHEDULE\nName of enactment | Number of sections | Punishment\n"
               "1. Section 498 | Two years\n2. Section 509 | Three years\n")],
        [(100, "THE SECOND SCHEDULE\nYear | Act | Extent of repeal\n"
               "1951 | Act XIV | The whole\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["29"]
    assert [n.label for n in parsed.nodes if n.kind == "schedule"] == ["FIRST", "SECOND"]
    assert "Section 498" in node(parsed, "/sch:FIRST").own_text
    assert "Act XIV" in node(parsed, "/sch:SECOND").own_text
    assert_exact(parsed, doc)


def test_wrapped_cross_references_are_not_new_section_headings():
    doc = document("The Example Act, 1960", [
        [(100, "8. Power. The power is subject to\nsection 8.] of the prior Act.\n"
               "11. Duty. This refers to\nsection 11-B of this Act.\n"
               "11-B. New duty. This is a real amended section.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["8", "11", "11-B"]
    assert "section 8.]" in node(parsed, "/s:8").own_text
    assert "section 11-B" in node(parsed, "/s:11").own_text
    assert_exact(parsed, doc)


def test_spaced_source_note_stays_separate_from_chapter_and_paragraph():
    doc = document("The Example Prosecution Act, 2006", [
        [(120, "CHAPTER-III\n13. Duties.\n(e) Perform work of the Government.\n"),
         (640, "CHAPTER-IV\nESTABLISHMENT\n"),
         (715, "1 Substituted for the words supervision and control by the Amendment Act\n"),
         (745, "2017.\n2Substituted for earlier words.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "chapter"] == ["III", "IV"]
    assert node(parsed, "/ch:III/s:13/p:e").own_text.strip().endswith("Government.")
    notes = [n for n in parsed.nodes if n.heading and n.heading.startswith("Source note")]
    assert [n.label for n in notes] == ["1", "2"]
    assert "2017." in notes[0].own_text
    assert_exact(parsed, doc)


def test_ambiguous_lowercase_ell_is_number_one_only_with_adjacent_two():
    doc = document("The Example Walls Act, 1995", [
        [(120, "1. Short title. (l) This Act may be called the Example Act.\n"
               "(2) It extends to the province.\n(3) It commences at once.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "subsection"] == ["1", "2", "3"]
    assert node(parsed, "/s:1/sub:1").own_text.startswith("(l)")
    assert any(w["code"] == "ambiguous_numbering_glyph" for w in parsed.warnings)
    assert_exact(parsed, doc)


def test_standalone_ambiguous_ell_uses_number_sequence_context():
    doc = document("The Example Act, 1995", [
        [(120, "1. Short title.\n(l) This Act has a short title.\n"
               "(2) It extends to the province.\n")],
    ])
    parsed = parse_structure([doc])
    assert [n.label for n in parsed.nodes if n.kind == "subsection"] == ["1", "2"]
    assert node(parsed, "/s:1/sub:1").own_text.startswith("(l)")
    assert_exact(parsed, doc)
