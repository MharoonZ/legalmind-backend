"""Source-explicit non-operative labels without PDF- or Act-specific rules."""

from uuid import uuid4

import pytest

from app.services.legal_structure import (
    SourceBlock, SourceDocument, SourcePage, parse_structure,
)


def source(body: str, notes: str = "") -> SourceDocument:
    file_id, extraction_id, page_id = uuid4(), uuid4(), uuid4()
    blocks = [SourceBlock(uuid4(), page_id, extraction_id, file_id, 1, 1,
                          "paragraph", body, {"x0": 70, "y0": 100, "x1": 520, "y1": 320}, 842)]
    if notes:
        blocks.append(SourceBlock(uuid4(), page_id, extraction_id, file_id, 1, 2,
                                  "footnote", notes,
                                  {"x0": 70, "y0": 690, "x1": 520, "y1": 760}, 842))
    page = SourcePage(page_id, extraction_id, file_id, 1, "extracted", [], 842, tuple(blocks))
    return SourceDocument(file_id, extraction_id, "The Example Act", "act", 1, (page,))


def node(parsed, kind: str, label: str):
    found = [item for item in parsed.nodes if item.kind == kind and item.label == label]
    assert len(found) == 1, [(item.kind, item.label) for item in parsed.nodes]
    return found[0]


def exact(parsed, document):
    blocks = {block.id: block for page in document.pages for block in page.blocks}
    nodes = {item.id: item for item in parsed.nodes}
    for span in parsed.spans:
        assert (nodes[span.provision_id].own_text[span.provision_char_start:span.provision_char_end]
                == blocks[span.block_id].text[span.block_char_start:span.block_char_end])


@pytest.mark.parametrize(("word", "status"), [
    ("Repealed", "repealed"),
    ("Omitted", "omitted"),
    ("Deleted", "deleted"),
])
def test_direct_status_on_section(word: str, status: str):
    document = source(f"25. [Former title]. {word} by the Amending Act.\n")
    parsed = parse_structure([document])
    assert node(parsed, "section", "25").source_status == status
    exact(parsed, document)


def test_star_only_subsection_status_from_exact_linked_note():
    document = source("6. Appointment.\n3[(3) * * * *]\n",
                      "3 Sub-section (3) omitted by the Amending Act.\n")
    parsed = parse_structure([document])
    child = node(parsed, "subsection", "3")
    assert child.source_status == "omitted"
    assert any(link.target_provision_id == child.id for link in parsed.note_links)
    exact(parsed, document)


def test_star_only_paragraph_status_from_exact_linked_note():
    document = source("28. Rules.\n(1) Government may provide:\n(a) active;\n4[(b) * * * *]\n",
                      "4 Paragraph (b) deleted by the Amending Act.\n")
    parsed = parse_structure([document])
    child = node(parsed, "paragraph", "b")
    assert child.source_status == "deleted"
    assert any(link.target_provision_id == child.id for link in parsed.note_links)
    exact(parsed, document)


def test_repealed_schedule_and_omitted_part_are_retained():
    document = source(
        "PART V\n[PERSONS UNDER SENTENCE]\nOmitted by the Amendment Order.\n"
        "THE THIRD SCHEDULE\n[Old form]. Repealed by the Repealing Act.\n"
    )
    parsed = parse_structure([document])
    assert node(parsed, "part", "V").source_status == "omitted"
    assert node(parsed, "schedule", "THIRD").source_status == "repealed"
    exact(parsed, document)


def test_grouped_heading_stays_one_source_faithful_repealed_node():
    document = source("2 and 3. [Repeals]. Repealed by the Repealing Act.\n")
    parsed = parse_structure([document])
    grouped = node(parsed, "section", "2 and 3")
    assert grouped.source_status == "repealed"
    assert [item.label for item in parsed.nodes if item.kind == "section"] == ["2 and 3"]
    exact(parsed, document)


@pytest.mark.parametrize("body", [
    "25. Power to amend.\n(3) Rules may be amended or repealed by resolution.\n",
    "2. Repeal.\nThe Sind Vagrancy Act is hereby repealed.\n",
])
def test_repeal_of_other_law_or_future_power_does_not_repeal_current_section(body: str):
    document = source(body)
    parsed = parse_structure([document])
    assert all(item.source_status is None for item in parsed.nodes if item.kind == "section")
    exact(parsed, document)


def test_ambiguous_note_does_not_guess_child_status():
    document = source("6. Appointment.\n3[(3) * * *]\n7. Other.\n3[(3) * * *]\n",
                      "3 Sub-section (3) omitted by an amending Act.\n")
    parsed = parse_structure([document])
    assert not [link for link in parsed.note_links if link.marker_label == "3"]
    assert all(item.source_status is None for item in parsed.nodes if item.kind == "subsection")
    assert any(w["code"] == "source_note_marker_ambiguous" for w in parsed.warnings)
    exact(parsed, document)
