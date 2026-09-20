"""Small, immutable value objects shared by extraction and database ingestion.

The PDF hash identifies the *file*.  The corpus hash also includes our
transcription and page split.  If a reviewer corrects any text, the corpus hash
and all affected evidence IDs change instead of silently changing an old
citation's meaning.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


def sha256_text(value: str) -> str:
    """Hash UTF-8 text, which is the representation stored in PostgreSQL."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PagePassage:
    page_number: int
    text: str
    evidence_id: str
    text_sha256: str


@dataclass(frozen=True)
class LawSection:
    section_number: int
    title: str
    text: str
    pdf_page_start: int
    pdf_page_end: int
    section_id: str
    text_sha256: str
    passages: tuple[PagePassage, ...]


@dataclass(frozen=True)
class CorpusVersion:
    source_id: str
    law_name: str
    jurisdiction: str
    source_url: str
    pdf_sha256: str
    corpus_sha256: str
    pdf_page_count: int
    review_status: str
    sections: tuple[LawSection, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready copy without changing the frozen value objects."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CorpusVersion:
        """Rebuild a corpus from JSON and reject edited or incomplete IDs."""
        sections = tuple(
            LawSection(
                **{
                    **section,
                    "passages": tuple(PagePassage(**item) for item in section["passages"]),
                }
            )
            for section in data["sections"]
        )
        corpus = cls(**{**data, "sections": sections})
        corpus.validate()
        return corpus

    def validate(self) -> None:
        """Fail closed before an untrusted/hand-edited JSON file reaches the DB."""
        if self.review_status != "needs_review":
            raise ValueError("Importer accepts only needs_review corpus data")
        if len(self.pdf_sha256) != 64 or len(self.corpus_sha256) != 64:
            raise ValueError("PDF and corpus hashes must be full SHA-256 values")
        if [s.section_number for s in self.sections] != list(range(1, 31)):
            raise ValueError("Corpus must contain sections 1 through 30 exactly once")

        digest_input = {
            "pdf_sha256": self.pdf_sha256,
            "sections": [
                {
                    "section_number": section.section_number,
                    "title": section.title,
                    "passages": [
                        {"page_number": p.page_number, "text": p.text}
                        for p in section.passages
                    ],
                }
                for section in self.sections
            ],
        }
        expected_corpus_hash = sha256_text(
            json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        if self.corpus_sha256 != expected_corpus_hash:
            raise ValueError("Corpus hash does not match its section text and page splits")
        if self.source_id != f"ca1872:{self.corpus_sha256}":
            raise ValueError("Source ID does not match the corpus hash")

        for section in self.sections:
            if not section.title or not section.passages:
                raise ValueError(f"Section {section.section_number} has no title or passages")
            pages = [p.page_number for p in section.passages]
            if pages != list(range(pages[0], pages[-1] + 1)):
                raise ValueError(f"Section {section.section_number} has duplicate or missing pages")
            if pages[0] < 1 or pages[-1] > self.pdf_page_count:
                raise ValueError(f"Section {section.section_number} lies outside the PDF")
            if (section.pdf_page_start, section.pdf_page_end) != (pages[0], pages[-1]):
                raise ValueError(f"Section {section.section_number} has incorrect page bounds")
            joined_text = "\n\n".join(p.text for p in section.passages)
            if section.text != joined_text or section.text_sha256 != sha256_text(joined_text):
                raise ValueError(f"Section {section.section_number} text hash mismatch")
            if section.section_id != f"{self.source_id}:s{section.section_number:02d}":
                raise ValueError(f"Section {section.section_number} ID mismatch")
            for passage in section.passages:
                if not passage.text or passage.text_sha256 != sha256_text(passage.text):
                    raise ValueError(f"Section {section.section_number} passage hash mismatch")
                stable_key = (
                    f"{self.source_id}|{section.section_number}|"
                    f"{passage.page_number}|{passage.text_sha256}"
                )
                if passage.evidence_id != f"ev:{sha256_text(stable_key)}":
                    raise ValueError(f"Section {section.section_number} evidence ID mismatch")
