"""Reviewed extraction attempts preserve raw evidence and atomize table rows."""

import fitz
import pytest

from app.services.legal_extraction import (
    ExtractionConfig, ambiguous_one_candidates, apply_reviewed_edits, extract_page,
)
from app.services.legal_tables import atomize_table_region, reviewed_region_is_lossless
from app.workers.legal_extract import process_one


def test_numbered_table_row_group_splits_into_source_traceable_entries():
    region = {
        "column_count": 3,
        "rows": [
            [{"text": "Enactment"}, {"text": "Sections"}, {"text": "Punishment"}],
            [{"text": "1"}, {"text": "2"}, {"text": "3"}],
            [{"text": "Chapter VIII"},
             {"text": "1. Sections 143 and 144.\n2. Section 153-A"},
             {"text": "Two years imprisonment.\nFive years imprisonment and\nwhipping."}],
        ],
    }
    rows, last_enactment = atomize_table_region(region)
    assert last_enactment == "Chapter VIII"
    entries = [row for row in rows if row["row_type"] == "entry"]
    assert len(entries) == 2
    assert entries[0]["cells"] == ["Chapter VIII", "1. Sections 143 and 144.",
                                   "Two years imprisonment."]
    assert entries[1]["cells"] == ["Chapter VIII", "2. Section 153-A",
                                   "Five years imprisonment and\nwhipping."]
    for entry in entries:
        for column in (1, 2):
            span = entry["source_slices"][column]
            original = region["rows"][2][column]["text"]
            assert original[span["start"]:span["end"]] == entry["cells"][column]


def test_multiline_table_with_empty_header_cells_is_lossless_only_when_exact():
    region = {
        "column_count": 3,
        "row_count": 3,
        "rows": [
            [{"text": "Enactment"}, {"text": "Sections"}, {"text": "Punishment"}],
            [{"text": "PART I"}, {"text": None}, {"text": None}],
            [{"text": "Chapter VIII"},
             {"text": "1. Section 143\n2. Section 144"},
             {"text": "Two years imprisonment.\nFive years imprisonment\nand whipping."}],
        ],
    }
    atoms, _ = atomize_table_region(region)
    reviewed = {**region, "status": "visually_reviewed_rows", "reviewed_by": "Reviewer",
                "review_note": "Checked each printed row and cell", "atomic_rows": atoms}
    assert atoms[1]["cells"] == ["PART I", "", ""]
    assert reviewed_region_is_lossless(reviewed)
    # This exact legacy data defect left two historical table regions blocked.
    legacy = {**reviewed, "atomic_rows": [
        {**item, "cells": ["PART I", None, None]} if item["source_row_index"] == 1
        else item for item in atoms
    ]}
    assert not reviewed_region_is_lossless(legacy)
    assert not reviewed_region_is_lossless({**reviewed, "row_count": 4})


def test_table_does_not_invent_alignment_when_item_counts_differ():
    region = {
        "column_count": 3,
        "rows": [
            [{"text": "Enactment"}, {"text": "Sections"}, {"text": "Punishment"}],
            [{"text": "1"}, {"text": "2"}, {"text": "3"}],
            [{"text": "Chapter VIII"},
             {"text": "1. Section 143\n2. Section 144"},
             {"text": "One punishment only."}],
        ],
    }
    with pytest.raises(ValueError, match="column 3 has no matching boundaries"):
        atomize_table_region(region)


@pytest.mark.parametrize("columns,source,expected", [
    (2, ["1. Section 3\n2. Section 4", "One year.\nTwo years."], 2),
    (4, ["Chapter III", "1. Section 3\n2. Section 4",
         "One year.\nTwo years.", "Fine.\nImprisonment."], 2),
    (4, ["Chapter III", "1. Section 3\n2. Section 4",
         "One year.\nTwo years.", ""], 2),
])
def test_future_table_shapes_split_without_document_specific_rules(columns, source, expected):
    region = {"column_count": columns, "rows": [
        [{"text": f"Heading {index}"} for index in range(columns)],
        [{"text": value} for value in source],
    ]}
    atomic, _ = atomize_table_region(region)
    entries = [item for item in atomic if item["source_row_index"] == 1]
    assert len(entries) == expected
    reviewed = {**region, "status": "visually_reviewed_rows", "reviewed_by": "Reviewer",
                "review_note": "Compared against the original page", "atomic_rows": atomic}
    assert reviewed_region_is_lossless(reviewed)
    entries[-1]["cells"][-1] += " invented"
    assert not reviewed_region_is_lossless(reviewed)


def test_unaligned_future_table_stays_unreviewed():
    region = {"column_count": 4, "rows": [
        [{"text": "Group"}, {"text": "Items"}, {"text": "Penalty"}, {"text": "Notes"}],
        [{"text": "Chapter"}, {"text": "1. Section 3\n2. Section 4"},
         {"text": "One year.\nTwo years."}, {"text": "One undivided note"}],
    ]}
    with pytest.raises(ValueError, match="column 4 has no matching boundaries"):
        atomize_table_region(region)


def test_glyph_candidate_detection_handles_other_future_pages_without_changing_text():
    assert ambiguous_one_candidates("8. Application.\n(l) One.\n(2) Two.")
    assert ambiguous_one_candidates("8. Short title.\n(I) This Act may be called X.")
    assert not ambiguous_one_candidates("(a) First\n(k) Earlier\n(l) Letter L\n(m) Later")


def test_visual_glyph_correction_preserves_raw_text_and_offsets():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 80), "1. Short title. (l) This Act may be called Example Act.")
    page.insert_text((50, 100), "(2) It extends to Punjab.")
    extracted = extract_page(page, 1, ExtractionConfig())
    ordinal = next(block.ordinal for block in extracted.blocks
                   if "(l) This Act may be called" in block.normalized_text)
    corrected = apply_reviewed_edits(extracted, {
        "text_replacements": [{"block_ordinal": ordinal,
                               "original": "(l) This Act may be called",
                               "replacement": "(1) This Act may be called"}],
    }, reviewer="Visual source reviewer")
    assert "(l) This Act may be called" in corrected.raw_text
    assert "(1) This Act may be called" in corrected.normalized_text
    assert corrected.outcome == "extracted"
    assert any(item["code"] == "visually_corrected_glyph" for item in corrected.warnings)
    for block in corrected.blocks:
        if block.page_char_start is not None:
            assert corrected.normalized_text[block.page_char_start:block.page_char_end] == block.normalized_text
    with pytest.raises(ValueError, match="length-preserving"):
        apply_reviewed_edits(extracted, {"text_replacements": [{
            "block_ordinal": ordinal, "original": "(l)", "replacement": "(12)",
        }]}, reviewer="Visual source reviewer")
    document.close()


def test_one_glyph_review_does_not_clear_a_second_unreviewed_candidate():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 80), "1. Title. (l) First title.")
    page.insert_text((50, 100), "2. Extent. (I) It extends to Punjab.")
    extracted = extract_page(page, 1, ExtractionConfig())
    first = next(block for block in extracted.blocks if "(l) First title" in block.normalized_text)
    corrected = apply_reviewed_edits(extracted, {"text_replacements": [{
        "block_ordinal": first.ordinal, "original": "(l) First title",
        "replacement": "(1) First title",
    }]}, reviewer="Visual source reviewer")
    assert corrected.outcome == "needs_review"
    assert any(item["code"] == "ambiguous_numbering_glyph" for item in corrected.warnings)
    assert "(I) It extends" in corrected.normalized_text
    document.close()


def test_reviewer_can_confirm_a_real_letter_l_without_rewriting_source():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 80), "1. Interpretation. (l) is the printed letter here.")
    extracted = extract_page(page, 1, ExtractionConfig())
    ordinal = next(block.ordinal for block in extracted.blocks if "(l)" in block.normalized_text)
    confirmed = apply_reviewed_edits(extracted, {
        "confirm_numbering_glyph_blocks": [ordinal],
    }, reviewer="Visual source reviewer")
    assert confirmed.normalized_text == extracted.normalized_text
    assert confirmed.outcome == "extracted"
    assert any(item["code"] == "numbering_glyph_confirmed_as_printed"
               for item in confirmed.warnings)
    document.close()


@pytest.mark.anyio
async def test_review_manifest_targets_matching_queued_pdf_only(tmp_path):
    source_hash = "a" * 64

    class EmptyQueue:
        async def claim_next(self, **kwargs):
            assert kwargs["expected_sha256"] == source_hash
            return None

    assert not await process_one(EmptyQueue(), upload_dir=str(tmp_path),
                                 config=ExtractionConfig(review_manifest={
                                     "source_sha256": source_hash, "pages": {},
                                 }))
    with pytest.raises(ValueError, match="SHA-256"):
        await process_one(EmptyQueue(), upload_dir=str(tmp_path),
                          config=ExtractionConfig(review_manifest={"pages": {}}))
