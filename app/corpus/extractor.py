"""Deterministically extract the sprint's thirty sections from one pinned PDF.

This is a transcription aid, not a legal approval step.  The resulting JSON is
always marked ``needs_review``.  We keep passages per PDF page so later quotes
can be checked against both the section and the *actual* page displayed to a
user.  Font-size filtering removes amendment footnotes and page furniture,
while retaining the section body and illustrations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import fitz

from app.corpus.models import CorpusVersion, LawSection, PagePassage, sha256_text

SOURCE_URL = (
    "https://pakistancode.gov.pk/pdffiles/"
    "administrator8332a6df32386960ac7d81a5cf7aade2.pdf"
)
EXPECTED_PDF_SHA256 = "930c60f67216530fab28f37d439ad3c3ae941dc918c914f2e7c18ddbabb7bfbd"
FIRST_BODY_PAGE = 11
LAST_BODY_PAGE = 24

# A bracket may precede an amended section (for example [16.).  Inserted
# lettered sections are kept separate from their numeric neighbors; the
# 30A heading is the stopping boundary for this sprint's numbered sections.
SECTION_START = re.compile(r"^\[?(\d{1,2}[A-Z]?)\.\s+")


@dataclass(frozen=True)
class TextLine:
    page: int
    y: float
    text: str
    bold_text: str


class ContractActExtractor:
    """Read the pinned Contract Act PDF and construct an immutable draft corpus."""

    def __init__(self, expected_pdf_sha256: str = EXPECTED_PDF_SHA256) -> None:
        self.expected_pdf_sha256 = expected_pdf_sha256.lower()

    @staticmethod
    def _join_spans(spans: list[dict]) -> str:
        """Reinsert a space when a filtered footnote marker separated words."""
        result = ""
        previous_right: float | None = None
        for span in spans:
            text = span["text"]
            if previous_right is not None and span["bbox"][0] - previous_right > 2:
                if result and not result[-1].isspace() and not text[0].isspace():
                    result += " "
            result += text
            previous_right = span["bbox"][2]
        return " ".join(result.split())

    def _page_lines(self, page: fitz.Page, page_number: int) -> list[TextLine]:
        # PyMuPDF's DICT representation includes each text span's size, font,
        # and bounding box.  This PDF uses 12 pt for the Act and 8 pt for
        # amendment footnotes, so the separation is source-specific by design.
        raw_lines: list[tuple[float, float, list[dict]]] = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [
                    span
                    for span in line["spans"]
                    if span["size"] >= 10 and span["text"].strip()
                ]
                if spans:
                    raw_lines.append((line["bbox"][1], line["bbox"][0], spans))

        # A list marker such as "(a)" and its prose may be separate PDF lines
        # at virtually the same vertical position.  Group them into one line.
        raw_lines.sort(key=lambda item: (item[0], item[1]))
        groups: list[list[tuple[float, float, list[dict]]]] = []
        for item in raw_lines:
            if groups and abs(item[0] - groups[-1][0][0]) <= 2.2:
                groups[-1].append(item)
            else:
                groups.append([item])

        lines: list[TextLine] = []
        for group in groups:
            spans = [span for _, _, parts in group for span in parts]
            spans.sort(key=lambda span: span["bbox"][0])
            text = self._join_spans(spans)
            if not text or re.fullmatch(r"Page \d+ of \d+|_+", text):
                continue
            bold = self._join_spans([s for s in spans if "Bold" in s["font"]])
            lines.append(TextLine(page_number, group[0][0], text, bold))
        return lines

    @staticmethod
    def _fragment_text(lines: list[TextLine]) -> str:
        """Join PDF wraps while retaining visible paragraph/subclause gaps."""
        out = ""
        previous_y: float | None = None
        for line in lines:
            gap = line.y - previous_y if previous_y is not None else 0
            # Preserve a printed line-end hyphen and its line break.  Joining
            # it with a space would fabricate text such as "in- fluence";
            # deleting it could change a genuine compound like
            # "subject-matter".  A reviewer can settle those cases later.
            separator = "\n" if gap > 19 or out.endswith("-") else " "
            if out:
                out += separator
            out += line.text
            previous_y = line.y
        return out.strip()

    @staticmethod
    def _title(lines: list[TextLine], number: int) -> str:
        # The title can wrap, most notably in section 25.  The PDF prints
        # section headings in bold, so collect consecutive bold title spans.
        pieces: list[str] = []
        for line in lines:
            if not line.bold_text:
                break
            pieces.append(line.bold_text)
            if line.text != line.bold_text and len(pieces) > 1:
                break
            if line.text != line.bold_text and re.search(r"\.\s+\S", line.text):
                break
        title = " ".join(pieces)
        title = re.sub(rf"^\[?{number}\.\s*", "", title).strip().rstrip(".")
        if not title:
            raise ValueError(f"Could not read title for section {number}")
        return title

    def extract(self, pdf_path: Path) -> CorpusVersion:
        pdf_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        if pdf_sha != self.expected_pdf_sha256:
            raise ValueError(
                "PDF hash differs from the pinned Pakistan Code file; "
                f"expected {self.expected_pdf_sha256}, got {pdf_sha}"
            )

        document = fitz.open(pdf_path)
        if len(document) < LAST_BODY_PAGE:
            raise ValueError("PDF ends before the expected section 30 boundary")

        by_section: dict[int, list[TextLine]] = defaultdict(list)
        current_section: int | None = None
        next_section = 1
        in_chapter_heading = False
        stopped_at_30a = False

        for page_number in range(FIRST_BODY_PAGE, LAST_BODY_PAGE + 1):
            for line in self._page_lines(document[page_number - 1], page_number):
                match = SECTION_START.match(line.text)
                if match:
                    label = match.group(1)
                    if label == "30A":
                        stopped_at_30a = True
                        break
                    if not label.isdigit():
                        # Section 19A sits between 19 and 20 in this PDF.  It
                        # is not section 19, so never append it to 19's text.
                        # The review guide flags its separate scope decision.
                        current_section = None
                        continue
                    if int(label) == next_section:
                        current_section = int(label)
                        next_section += 1
                        in_chapter_heading = False
                    elif int(label) <= 30:
                        raise ValueError(f"Unexpected section {label} on PDF page {page_number}")

                # This centered heading categorizes the following provisions;
                # it is not part of section 23's text.
                # The source actually prints "Viod Agreements" here.  Keep
                # both spellings in this source-specific skip rule so a later
                # corrected PDF still excludes the non-statutory heading.
                if line.text in {"Void Agreements", "Viod Agreements"}:
                    continue
                if line.text.startswith("CHAPTER "):
                    in_chapter_heading = True
                if current_section is not None and not in_chapter_heading:
                    by_section[current_section].append(line)
            if stopped_at_30a:
                break

        if not stopped_at_30a or next_section != 31:
            raise ValueError("Did not find every section 1-30 followed by section 30A")

        raw_sections: list[tuple[int, str, list[tuple[int, str]]]] = []
        for number in range(1, 31):
            lines = by_section[number]
            if not lines:
                raise ValueError(f"Section {number} has no extracted text")
            page_groups: dict[int, list[TextLine]] = defaultdict(list)
            for line in lines:
                page_groups[line.page].append(line)
            fragments = [
                (page, self._fragment_text(page_groups[page]))
                for page in sorted(page_groups)
            ]
            raw_sections.append((number, self._title(lines, number), fragments))

        digest_input = {
            "pdf_sha256": pdf_sha,
            "sections": [
                {
                    "section_number": number,
                    "title": title,
                    "passages": [
                        {"page_number": page, "text": text}
                        for page, text in fragments
                    ],
                }
                for number, title, fragments in raw_sections
            ],
        }
        corpus_sha = sha256_text(
            json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        source_id = f"ca1872:{corpus_sha}"
        sections: list[LawSection] = []
        for number, title, fragments in raw_sections:
            passages = tuple(
                PagePassage(
                    page_number=page,
                    text=text,
                    text_sha256=sha256_text(text),
                    evidence_id=f"ev:{sha256_text(f'{source_id}|{number}|{page}|{sha256_text(text)}')}",
                )
                for page, text in fragments
            )
            section_text = "\n\n".join(p.text for p in passages)
            sections.append(
                LawSection(
                    section_number=number,
                    title=title,
                    text=section_text,
                    pdf_page_start=passages[0].page_number,
                    pdf_page_end=passages[-1].page_number,
                    section_id=f"{source_id}:s{number:02d}",
                    text_sha256=sha256_text(section_text),
                    passages=passages,
                )
            )

        corpus = CorpusVersion(
            source_id=source_id,
            law_name="Contract Act, 1872",
            jurisdiction="Pakistan",
            source_url=SOURCE_URL,
            pdf_sha256=pdf_sha,
            corpus_sha256=corpus_sha,
            pdf_page_count=len(document),
            review_status="needs_review",
            sections=tuple(sections),
        )
        corpus.validate()
        return corpus
