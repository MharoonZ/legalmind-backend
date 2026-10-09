"""Generic legal-structure failures observed in three pilot PDFs.

The fixtures deliberately contain no document-specific parser exceptions.
"""

from uuid import uuid4

from app.services.legal_structure import SourceBlock, SourceDocument, SourcePage, parse_structure


def document(*blocks: tuple[int, str], title: str = "The Example Act") -> SourceDocument:
    file_id, extraction_id, page_id = uuid4(), uuid4(), uuid4()
    source_blocks = tuple(
        SourceBlock(uuid4(), page_id, extraction_id, file_id, 1, index, "paragraph",
                    text, {"x0": 72, "y0": y, "x1": 520, "y1": y + 24}, 842)
        for index, (y, text) in enumerate(blocks, 1)
    )
    page = SourcePage(page_id, extraction_id, file_id, 1, "extracted", [], 842, source_blocks)
    return SourceDocument(file_id, extraction_id, title, "act", 1, (page,))


def unique(parsed, kind: str, label: str):
    matches = [n for n in parsed.nodes if n.kind == kind and n.label == label]
    assert len(matches) == 1, [(n.kind, n.label, n.reference_path) for n in parsed.nodes]
    return matches[0]


def exact(parsed, source):
    blocks = {b.id: b for page in source.pages for b in page.blocks}
    nodes = {n.id: n for n in parsed.nodes}
    for span in parsed.spans:
        assert nodes[span.provision_id].own_text[span.provision_char_start:span.provision_char_end] == (
            blocks[span.block_id].text[span.block_char_start:span.block_char_end]
        )
    assert parsed.coverage["partial_blocks"] == parsed.coverage["unmapped_blocks"] == 0


def test_same_page_contents_to_body_and_no_space_inline_first_subsection():
    source = document(
        (90, "CONTENTS\n1. Short title\n2. Application\n"),
        (250, "THE EXAMPLE ACT\nAn Act to regulate examples.\nPreamble. WHEREAS regulation is necessary.\n"),
        (380, "1. Short title.\u2013 (1)This Act may be called the Example Act.\n"
               "(2) It shall commence at once.\n(3) It applies generally.\n"),
        (540, "2. Application.\u2013 The Act applies to every person.\n"),
    )
    parsed = parse_structure([source])
    first, second = unique(parsed, "section", "1"), unique(parsed, "section", "2")
    assert first.parent_id == second.parent_id is None
    assert [n.label for n in parsed.nodes if n.kind == "subsection" and n.parent_id == first.id] == ["1", "2", "3"]
    assert not any("Preamble" in n.own_text for n in parsed.nodes if n.heading == "Contents continuation")
    exact(parsed, source)


def test_parent_continuation_and_proviso_do_not_corrupt_siblings():
    source = document(
        (110, "3. Penalty.\u2013 (1) A person who\n(a) acts unlawfully;\n"
              "(b) assists another; or\n(c) conceals evidence,\n"
              "shall be punishable by imprisonment.\n"),
        (440, "8. Procedure.\u2013 (1) The officer may\n(a) inspect premises.\n"
              "Provided that a warrant is required.\n(b) demand records;\n"
              "(c) seize records.\n"),
    )
    parsed = parse_structure([source])
    sub3, sub8 = (next(n for n in parsed.nodes if n.kind == "subsection" and n.parent_id == unique(parsed, "section", label).id)
                  for label in ("3", "8"))
    assert "shall be punishable" in sub3.own_text
    assert all("shall be punishable" not in n.own_text for n in parsed.nodes
               if n.kind == "paragraph" and n.parent_id == sub3.id)
    assert [n.label for n in parsed.nodes if n.kind == "paragraph" and n.parent_id == sub8.id] == ["a", "b", "c"]
    assert any(n.heading == "Proviso" and n.parent_id == sub8.id for n in parsed.nodes)
    exact(parsed, source)


def test_inline_references_cannot_create_section_or_chapter_or_reparent_later_sections():
    source = document(
        (105, "6. Genuine section.\u2013 This is operative text.\n"),
        (205, "11. Later section.\u2013 The officer may act under\nsection 6. \n"),
        (315, "20. Penalty.\u2013 (1) A person punished under\n"
              "Chapter XVI of the Pakistan Penal Code\nmay be detained.\n"
              "(2) A second condition applies.\n(3) A third condition applies.\n"),
        (590, "21. Next section.\u2013 This is another operative provision.\n"),
    )
    parsed = parse_structure([source])
    assert [n.label for n in parsed.nodes if n.kind == "section"] == ["6", "11", "20", "21"]
    assert not [n for n in parsed.nodes if n.kind == "chapter"]
    section20 = unique(parsed, "section", "20")
    assert [n.label for n in parsed.nodes if n.kind == "subsection" and n.parent_id == section20.id] == ["1", "2", "3"]
    assert unique(parsed, "section", "21").parent_id is None
    exact(parsed, source)


def test_explicit_omission_and_embedded_note_markers():
    source = document(
        (105, "12. 1[* * *]\n"),
        (310, "13. Application.\u2013 2[Punjab] is named in the provision.\n"
              "5[(2) An added subsection remains operative.]\n"),
        (625, "1 Omitted by an amending Act.\n2 Substituted by an amending Act.\n"
              "5 Added by an amending Act.\n"),
    )
    parsed = parse_structure([source])
    section = unique(parsed, "section", "12")
    assert section.source_status == "omitted"
    assert unique(parsed, "subsection", "2").parent_id == unique(parsed, "section", "13").id
    assert {(link.marker_label, link.target_provision_id) for link in parsed.note_links} >= {
        ("1", section.id), ("2", unique(parsed, "section", "13").id),
    }
    assert all(n.source_status is None for n in parsed.nodes if n.kind != "section")
    exact(parsed, source)


def test_colon_after_first_item_does_not_nest_sequential_siblings():
    source = document((120, "19. Enquiry.\u2013 (1) The person may\n"
                           "(a) show cause:\n(b) examine witnesses;\n"
                           "(c) offer evidence.\n"))
    parsed = parse_structure([source])
    subsection = unique(parsed, "subsection", "1")
    assert [n.label for n in parsed.nodes if n.kind == "paragraph" and n.parent_id == subsection.id] == ["a", "b", "c"]
    exact(parsed, source)


def test_subject_and_modal_after_child_list_returns_to_parent():
    source = document((120, "17. Security.\u2013 (1) Where the officer\n"
                           "(a) finds a threat; or\n(b) receives a report,\n"
                           "the District Officer may require security.\n"))
    parsed = parse_structure([source])
    subsection = unique(parsed, "subsection", "1")
    assert "the District Officer may" in subsection.own_text
    assert "the District Officer may" not in unique(parsed, "paragraph", "b").own_text
    exact(parsed, source)


def test_list_context_survives_across_extracted_blocks():
    source = document(
        (120, "22. Fine.\u2013 (1) Where a village\n(a) fails to report; or\n"
              "(b) shelters an offender,\n"),
        (300, "the District Officer may impose a fine.\n(2) The fine shall be reviewed.\n"),
    )
    parsed = parse_structure([source])
    subsection = unique(parsed, "subsection", "1")
    assert "the District Officer may" in subsection.own_text
    assert "the District Officer may" not in unique(parsed, "paragraph", "b").own_text
    exact(parsed, source)


def test_exclusion_phrase_after_comma_stays_with_definition_and_opens_nested_list():
    source = document(
        (120, "2. Definitions.\n(h) manufacturer means a person who-\n"
              "(i) makes a product;\n(ii) supplies a product.\n"
              "(i) manufacturing means making a product;\n"
              "(k) services include advice;\n"),
        (410, "(l) anticipated alteration means a change arising from ordinary wear,\n"),
        (500, "but does not include-\n"
              "(i) failure to maintain the product;\n"
              "(ii) removal of a warning;\n"
              "(iii) misuse of the product;\n"
              "(m) anticipated use means ordinary use.\n"),
    )
    parsed = parse_structure([source])
    section = unique(parsed, "section", "2")
    definition = unique(parsed, "paragraph", "l")
    assert definition.parent_id == section.id
    assert "but does not include" in definition.own_text
    assert [n.label for n in parsed.nodes
            if n.kind == "paragraph" and n.parent_id == definition.id] == ["i", "ii", "iii"]
    assert any(n.kind == "paragraph" and n.label == "i" and n.parent_id == section.id
               for n in parsed.nodes)
    exact(parsed, source)


def test_source_explicit_repeal_is_status_not_a_guess_about_current_law():
    source = document((110, "14. [Repealed by the Amending Act.]\n"))
    parsed = parse_structure([source])
    assert unique(parsed, "section", "14").source_status == "repealed"
    exact(parsed, source)


def test_same_note_marker_on_different_provisions_is_left_for_review():
    source = document(
        (110, "1. 2[First wording.]\n2. 2[Second wording.]\n"),
        (650, "2 Substituted by an amending Act.\n"),
    )
    parsed = parse_structure([source])
    assert not [link for link in parsed.note_links if link.marker_label == "2"]
    assert any(w["code"] == "source_note_marker_ambiguous" for w in parsed.warnings)
    exact(parsed, source)
