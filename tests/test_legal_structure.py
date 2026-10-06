"""The draft parser preserves full identifiers, hierarchy, and exact provenance."""

from uuid import uuid4

from app.services.legal_structure import (
    SourceBlock, SourceDocument, SourcePage, parse_structure,
)


def source(lines: list[list[str]], *, instrument_type="act", title="Example Act",
           file_id=None, outcomes=None, kinds=None) -> SourceDocument:
    file_id = file_id or uuid4()
    extraction_id = uuid4()
    pages = []
    for page_index, texts in enumerate(lines, 1):
        page_id = uuid4()
        blocks = tuple(SourceBlock(
            id=uuid4(), page_id=page_id, extraction_id=extraction_id,
            file_id=file_id, page_index=page_index, ordinal=index,
            kind=kinds.get((page_index, index), "paragraph") if kinds else "paragraph",
            text=text, bbox={"x0": 20, "y0": 50 + index * 30, "x1": 500, "y1": 75 + index * 30},
            page_height=800,
        ) for index, text in enumerate(texts, 1))
        pages.append(SourcePage(page_id, extraction_id, file_id, page_index,
                                (outcomes or {}).get(page_index, "extracted"), [], 800, blocks))
    return SourceDocument(file_id, extraction_id, title, instrument_type,
                          len(lines), tuple(pages))


def assert_exact(result, documents):
    nodes = {node.id: node for node in result.nodes}
    blocks = {block.id: block for doc in documents for page in doc.pages for block in page.blocks}
    for span in result.spans:
        assert nodes[span.provision_id].own_text[span.provision_char_start:span.provision_char_end] == (
            blocks[span.block_id].text[span.block_char_start:span.block_char_end]
        )
    assert len(result.decisions) == len(blocks)
    assert len({node.reference_path for node in result.nodes}) == len(result.nodes)
    assert [node.ordinal for node in result.nodes] == list(range(1, len(result.nodes) + 1))


def test_sections_19_and_19a_are_distinct_with_nested_text():
    doc = source([["19. Main section. Introductory words.\n(1) First subsection.\n(a) First paragraph.\n19A. Later section. Distinct words."]])
    parsed = parse_structure([doc])
    numbered = [node for node in parsed.nodes if node.kind == "section"]
    assert [node.label for node in numbered] == ["19", "19A"]
    sub = next(node for node in parsed.nodes if node.kind == "subsection")
    para = next(node for node in parsed.nodes if node.kind == "paragraph")
    assert sub.parent_id == numbered[0].id
    assert para.parent_id == sub.id
    assert "First paragraph" not in numbered[0].own_text
    assert "First paragraph" not in sub.own_text
    assert "Distinct words" not in numbered[0].own_text
    assert_exact(parsed, [doc])


def test_rules_and_schedules_are_separate_and_references_not_truncated():
    doc = source([["Rule 7(2). Detailed rule. See section 19A and the First Schedule.\nFIRST SCHEDULE\nSchedule text.\nSECOND SCHEDULE\nMore schedule text." ]], instrument_type="rule")
    parsed = parse_structure([doc])
    assert [node.label for node in parsed.nodes if node.kind == "rule"] == ["7(2)"]
    assert [node.label for node in parsed.nodes if node.kind == "schedule"] == ["FIRST", "SECOND"]
    assert any(ref.raw_reference.lower() == "section 19a" for ref in parsed.references)
    assert any("First Schedule" in ref.raw_reference for ref in parsed.references)
    assert_exact(parsed, [doc])


def test_repeated_headers_are_ignored_and_section_can_cross_pages():
    doc = source([
        ["1 Example Laws", "1. Scope. The first page starts a section."],
        ["2 Example Laws", "It continues on the next physical page.\n2. Next section. New words."],
    ], title="Example Laws")
    parsed = parse_structure([doc])
    sections = [node for node in parsed.nodes if node.kind == "section"]
    assert [node.label for node in sections] == ["1", "2"]
    assert len([span for span in parsed.spans if span.provision_id == sections[0].id]) == 2
    assert parsed.coverage["ignored_blocks"] == 2
    assert parsed.coverage["unmapped_blocks"] == 0
    assert_exact(parsed, [doc])


def test_table_ocr_noise_missing_pages_and_two_sources_are_reported():
    first = source([["1. First law text.\nBad glyph � in source."],
                    ["TABLE\nColumn A  Column B"]], kinds={(2, 1): "table"},
                   outcomes={2: "needs_review"})
    second = source([["1. Supplementary PDF with its own section." ]])
    parsed = parse_structure([first, second])
    assert [node.label for node in parsed.nodes if node.kind == "section"] == ["1", "1"]
    assert len({node.reference_path for node in parsed.nodes}) == len(parsed.nodes)
    assert parsed.coverage["total_pages"] == 3
    assert parsed.coverage["unmapped_blocks"] == 0
    codes = {item["code"] for item in parsed.warnings}
    assert {"source_page_unreviewed", "broken_text_encoding", "table_requires_review"} <= codes
    assert_exact(parsed, [first, second])
    damaged = SourceDocument(first.file_id, first.extraction_id, first.title, first.instrument_type,
                             3, first.pages)
    assert any(w["code"] == "missing_pages" for w in parse_structure([damaged]).warnings)


def test_side_by_side_blocks_raise_reading_order_warning():
    doc = source([["1. Left column of the law.", "2. Right column of the law."]])
    page = doc.pages[0]
    left, right = page.blocks
    from dataclasses import replace
    right = replace(right, bbox={"x0": 320, "y0": 82, "x1": 550, "y1": 125})
    left = replace(left, bbox={"x0": 20, "y0": 80, "x1": 290, "y1": 125})
    altered = replace(doc, pages=(replace(page, blocks=(left, right)),))
    parsed = parse_structure([altered])
    assert any(item["code"] == "possible_multi_column" for item in parsed.warnings)
    assert_exact(parsed, [altered])


def test_duplicate_identifier_is_preserved_with_warning_and_unique_path():
    doc = source([["19A. First occurrence.\n19A. Second occurrence."]])
    parsed = parse_structure([doc])
    sections = [node for node in parsed.nodes if node.kind == "section"]
    assert len(sections) == 2
    assert sections[0].reference_path.endswith("/s:19A")
    assert sections[1].reference_path.endswith("/s:19A/duplicate:2")
    assert any(item["code"] == "duplicate_identifier" for item in parsed.warnings)
    assert_exact(parsed, [doc])
