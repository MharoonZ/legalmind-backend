"""Regression cases from the rendered Punjab Vagrancy Ordinance, 1958.

The synthetic blocks retain the boundaries and amendment/footnote layout of
physical PDF pages 3-4. These tests exercise the deterministic draft parser,
not a claim that a draft has been legally approved.
"""

from uuid import uuid4

from app.services.legal_structure import (
    SourceBlock,
    SourceDocument,
    SourcePage,
    parse_structure,
)


def source(*pages: list[tuple[float, str]]) -> SourceDocument:
    file_id, extraction_id = uuid4(), uuid4()
    source_pages = []
    for page_index, specs in enumerate(pages, 1):
        page_id = uuid4()
        blocks = tuple(
            SourceBlock(
                id=uuid4(), page_id=page_id, extraction_id=extraction_id,
                file_id=file_id, page_index=page_index, ordinal=ordinal,
                kind="paragraph", text=text,
                bbox={"x0": 85, "y0": y, "x1": 510, "y1": min(y + 25, 790)},
                page_height=792,
            )
            for ordinal, (y, text) in enumerate(specs, 1)
        )
        source_pages.append(SourcePage(
            page_id, extraction_id, file_id, page_index,
            "extracted", [], 792, blocks,
        ))
    return SourceDocument(
        file_id, extraction_id, "The Punjab Vagrancy Ordinance, 1958",
        "ordinance", len(source_pages), tuple(source_pages),
    )


def node(result, suffix: str):
    matches = [item for item in result.nodes if item.reference_path.endswith(suffix)]
    assert len(matches) == 1, (suffix, [item.reference_path for item in matches])
    return matches[0]


def assert_exact_source_spans(result, document: SourceDocument) -> None:
    nodes = {item.id: item for item in result.nodes}
    blocks = {block.id: block for page in document.pages for block in page.blocks}
    for span in result.spans:
        assert nodes[span.provision_id].own_text[
            span.provision_char_start:span.provision_char_end
        ] == blocks[span.block_id].text[span.block_char_start:span.block_char_end]
    for link in result.note_links:
        assert blocks[link.block_id].text[
            link.block_char_start:link.block_char_end
        ] == link.marker_label


def test_amendment_prefixed_da_is_separate_and_marker_links_to_its_note():
    doc = source([
        (400, "2. Definitions.\n(d) guardian means the person in charge;\n"
              "9[(da) organizer means a person arranging vagrancy;\n"
              "(db) person includes a transgender person;]\n"
              "(e) prescribed means prescribed by rules;\n"
              "(f) public place includes any public park;\n"),
        (720, "9Inserted by the Punjab Vagrancy (Amendment) Act 2025.\n"),
    ])
    parsed = parse_structure([doc])
    section = node(parsed, "/s:2")
    children = [item for item in parsed.nodes if item.parent_id == section.id]
    assert [(item.kind, item.label) for item in children] == [
        ("paragraph", value) for value in ("d", "da", "db", "e", "f")
    ]
    assert node(parsed, "/s:2/p:d").own_text.strip().endswith("in charge;")
    da = node(parsed, "/s:2/p:da")
    assert da.own_text.startswith("9[(da)")
    assert node(parsed, "/s:2/p:db").own_text.strip().endswith("person;]")
    link = next(item for item in parsed.note_links if item.marker_label == "9")
    assert link.target_provision_id == da.id
    assert_exact_source_spans(parsed, doc)


def test_nine_page_footnotes_are_distinct_and_not_in_paragraph_f():
    # On physical page 3, footnote 1 starts in one PyMuPDF block and
    # continues into the block that also contains footnotes 2-9.
    doc = source([
        (72, "1THE PUNJAB VAGRANCY ORDINANCE, 1958\n"),
        (138, "An Ordinance relating to vagrancy in 2[the Punjab].\n"
              "Preamble. In the Province of 3[Punjab].\n"
              "1. Short title. The 4[Punjab] Vagrancy Ordinance.\n"
              "5[(2) It extends to the whole of 6[Punjab].]\n"
              "2. Definitions.\n(b) Controller of Vagrancy, 7[Punjab];\n"
              "(c) Government means 8[Provincial Government];\n"
              "(d) guardian means a person in charge;\n"
              "9[(da) organizer means a person arranging vagrancy;\n"
              "(db) person includes a transgender person;]\n"
              "(e) prescribed means prescribed by rules;\n"
              "(f) public place includes a place accessible to all, "),
        (608, "whether on payment or otherwise;\n"),
        (635, "1This Ordinance was promulgated by the Governor; published in the Gazette\n"),
        (660, "(Extraordinary), dated 1 Oct. 1958.\n"
              "2Substituted by the Punjab Laws (Adaptation) Order, 1974.\n"
              "3Substituted by the Punjab Laws (Adaptation) Order, 1974.\n"
              "4Substituted by the Punjab Laws (Adaptation) Order, 1974.\n"
              "5Substituted by the Extension Ordinance, 1964.\n"
              "6Substituted by the Punjab Laws (Adaptation) Order, 1974.\n"
              "7Substituted by the Punjab Laws (Adaptation) Order, 1974.\n"
              "8Substituted by the Punjab Laws (Adaptation) Order, 1974.\n"
              "9Inserted by the Punjab Vagrancy (Amendment) Act 2025.\n"),
    ])
    parsed = parse_structure([doc])
    paragraph_f = node(parsed, "/s:2/p:f")
    assert paragraph_f.own_text.strip().endswith("whether on payment or otherwise;")
    assert "This Ordinance was promulgated" not in paragraph_f.own_text
    notes = [item for item in parsed.nodes if item.heading and item.heading.startswith("Source note ")]
    assert [(item.label, item.heading) for item in notes] == [
        (str(number), f"Source note {number}") for number in range(1, 10)
    ]
    assert "(Extraordinary)" in notes[0].own_text
    assert "2Substituted" not in notes[0].own_text
    assert {item.marker_label for item in parsed.note_links} == {str(number) for number in range(1, 10)}
    assert next(item for item in parsed.note_links if item.marker_label == "9").target_provision_id == node(parsed, "/s:2/p:da").id
    assert_exact_source_spans(parsed, doc)


def test_roman_list_is_nested_under_g_and_post_list_continuation_returns_to_g():
    doc = source([
        (100, "2. Definitions.\n(g) vagrant means a person who\ufffd\n"
              "(i) solicits or receives alms in a public place;\n"
              "(ii) exposes a wound to solicit alms;\n"),
        (180, "(iii) allows himself to be used as an exhibit;\n"
              "(iv) enters private premises for soliciting or receiving alms;\n"
              "but does not include a person collecting under a certificate;\n"
              "(h) welfare home means an institution maintained by Government.\n"),
    ])
    parsed = parse_structure([doc])
    g = node(parsed, "/s:2/p:g")
    roman = [node(parsed, f"/s:2/p:g/p:{label}") for label in ("i", "ii", "iii", "iv")]
    assert all(item.parent_id == g.id for item in roman)
    assert roman[-1].own_text.strip().endswith("receiving alms;")
    assert "but does not include" not in roman[-1].own_text
    assert "but does not include a person" in g.own_text
    h = node(parsed, "/s:2/p:h")
    assert h.parent_id == node(parsed, "/s:2").id
    assert_exact_source_spans(parsed, doc)


def test_proviso_after_f_belongs_to_subsection_two_not_paragraph_f():
    doc = source([
        (100, "5. Duties of the manager.\n"
              "(2) The manager shall keep classes of vagrants separately:\n"
              "(a) children;\n(b) females;\n(f) able bodied:\n"
              "Provided that children less than seven years of age may reside with family.\n"
              "(3) The manager shall arrange education.\n"),
    ])
    parsed = parse_structure([doc])
    subsection_two = node(parsed, "/s:5/sub:2")
    f = node(parsed, "/s:5/sub:2/p:f")
    assert f.own_text.strip() == "(f) able bodied:"
    proviso_nodes = [item for item in parsed.nodes if "Provided that children" in item.own_text]
    assert len(proviso_nodes) == 1
    assert (proviso_nodes[0].id == subsection_two.id
            or proviso_nodes[0].parent_id == subsection_two.id)
    assert "Provided that" not in node(parsed, "/s:5/sub:3").own_text
    assert_exact_source_spans(parsed, doc)


def test_letter_i_after_h_is_not_misread_as_a_nested_roman_list():
    doc = source([
        (100, "25. Power to make rules.\n"
              "(2) The rules may provide for the following matters:\n"
              "(h) how a home certifies satisfactory employment;\n"
              "(i) the institutions where children are confined;\n"
              "(j) the institutions where lepers are confined.\n"),
    ])
    parsed = parse_structure([doc])
    subsection = node(parsed, "/s:25/sub:2")
    assert [node(parsed, f"/s:25/sub:2/p:{label}").parent_id for label in ("h", "i", "j")] == [
        subsection.id, subsection.id, subsection.id,
    ]
    assert_exact_source_spans(parsed, doc)


def test_sections_eight_and_nine_keep_their_own_provisos():
    doc = source([
        (100, "8. Trial. The person shall be tried under the Code:\n"
              "Provided that a successor magistrate may act on recorded evidence.\n"
              "9. Sentence for vagrancy. The magistrate may sentence the person:\n"
              "Provided that a first offender may be released after admonition.\n"
              "11. Presumption. A separate section follows.\n"),
    ])
    parsed = parse_structure([doc])
    eight, nine = node(parsed, "/s:8"), node(parsed, "/s:9")
    assert "successor magistrate" in eight.own_text
    assert "first offender" not in eight.own_text
    assert "first offender" in nine.own_text
    assert "successor magistrate" not in nine.own_text
    assert "first offender" not in node(parsed, "/s:11").own_text
    assert_exact_source_spans(parsed, doc)


def test_amendment_prefixed_sections_ten_nineteen_and_twenty_are_separate():
    # These prefixes are printed footnote/amendment markers on physical pages
    # 5 and 8, not part of the underlying section identifiers.
    doc = source(
        [
            (100, "9. Sentence for vagrancy. Earlier operative text.\n"
                  "3[10. Punishment for organizing alms.- (1) New operative text.\n"
                  "(a) A first category of offender.\n"),
        ],
        [
            (100, "18. Effect of other laws. Earlier operative text.\n"
                  "1[19. Cognizance and bail. Current operative text.]\n"
                  "2[20. Jurisdiction. Current operative text.]\n"
                  "21. Appeal. Later operative text.\n"),
        ],
    )
    parsed = parse_structure([doc])
    sections = [item for item in parsed.nodes if item.kind == "section"]
    assert [item.label for item in sections] == ["9", "10", "18", "19", "20", "21"]
    for label, printed_prefix in (("10", "3[10."), ("19", "1[19."), ("20", "2[20.")):
        section = node(parsed, f"/s:{label}")
        assert section.own_text.startswith(printed_prefix)
        assert section.parent_id is None
    assert "3[10." not in node(parsed, "/s:9").own_text
    assert "1[19." not in node(parsed, "/s:18").own_text
    assert "2[20." not in node(parsed, "/s:19").own_text
    assert "21. Appeal" not in node(parsed, "/s:20").own_text
    assert node(parsed, "/s:10/sub:1").parent_id == node(parsed, "/s:10").id
    assert node(parsed, "/s:10/sub:1/p:a").parent_id == node(parsed, "/s:10/sub:1").id
    assert_exact_source_spans(parsed, doc)
