"""Regression checks for the legal source boundary and stable citation IDs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.corpus.extractor import ContractActExtractor, EXPECTED_PDF_SHA256, SOURCE_URL
from app.corpus.models import CorpusVersion

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "data" / "sources" / "contract_act_1872_pakistan_code.pdf"
CORPUS = ROOT / "data" / "contract_act_1872_sections_1_30.json"


@pytest.fixture(scope="module")
def corpus() -> CorpusVersion:
    """Read the shipped manifest and verify all of its content-derived IDs."""
    return CorpusVersion.from_dict(json.loads(CORPUS.read_text(encoding="utf-8")))


def test_source_file_and_manifest_are_reproducible(corpus: CorpusVersion) -> None:
    rebuilt = ContractActExtractor().extract(PDF)
    assert rebuilt == corpus
    assert corpus.pdf_sha256 == EXPECTED_PDF_SHA256
    assert corpus.source_url == SOURCE_URL
    assert corpus.pdf_page_count == 65
    assert corpus.review_status == "needs_review"


def test_all_thirty_sections_have_correct_boundary_pages(corpus: CorpusVersion) -> None:
    assert [section.section_number for section in corpus.sections] == list(range(1, 31))
    assert sum(len(section.passages) for section in corpus.sections) == 41
    assert (corpus.sections[9].pdf_page_start, corpus.sections[9].pdf_page_end) == (14, 14)
    assert (corpus.sections[15].pdf_page_start, corpus.sections[15].pdf_page_end) == (15, 16)
    assert (corpus.sections[22].pdf_page_start, corpus.sections[22].pdf_page_end) == (19, 21)
    assert (corpus.sections[29].pdf_page_start, corpus.sections[29].pdf_page_end) == (23, 24)
    assert "Section 294A" in corpus.sections[29].passages[-1].text
    assert "19A." not in corpus.sections[18].text
    assert "30A." not in corpus.sections[29].text
    assert "CHAPTER" not in " ".join(section.text for section in corpus.sections)
    assert "Void Agreements" not in corpus.sections[22].text
    assert "Viod Agreements" not in corpus.sections[22].text


def test_illustrations_and_amended_section_heading_are_retained(corpus: CorpusVersion) -> None:
    assert "Illustrations" in corpus.sections[11].text  # section 12
    assert "Illustrations" in corpus.sections[15].text  # section 16
    assert "Illustrations" in corpus.sections[28].text  # section 29
    assert corpus.sections[15].text.startswith("[16. “Undue influence” defined")
    assert not any("Page 15 of 65" in passage.text for passage in corpus.sections[15].passages)


def test_wrong_pdf_is_rejected(tmp_path: Path) -> None:
    wrong_pdf = tmp_path / "wrong.pdf"
    wrong_pdf.write_bytes(b"This is not the pinned legal source")
    with pytest.raises(ValueError, match="PDF hash differs"):
        ContractActExtractor().extract(wrong_pdf)


def test_edited_manifest_cannot_reuse_existing_evidence_ids(corpus: CorpusVersion) -> None:
    changed = corpus.to_dict()
    changed["sections"][9]["passages"][0]["text"] += " invented text"
    with pytest.raises(ValueError, match="Corpus hash"):
        CorpusVersion.from_dict(changed)
