"""Page-faithful PDF extraction for the reviewed legal corpus.

No section parsing happens here. Every physical page receives an outcome, and
OCR output is deliberately marked for human review rather than trusted as law.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import fitz

from app.services.legal_tables import atomize_table_region, reviewed_region_is_lossless


EXTRACTOR_VERSION = "legal-pages-v4"
_OBJECT_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.pdf$")
_AMBIGUOUS_MARKER = re.compile(r"\([lI]\)[ \t]+")
_NUMBERED_HEADING = re.compile(r"(?m)^[ \t]*\d+(?:-[A-Za-z]+|[A-Za-z]*)\.[ \t]+")
_SECOND_SUBSECTION = re.compile(r"\(2\)[ \t]+")
_PREVIOUS_LETTER_ITEM = re.compile(r"\([a-k]\)[ \t]+", re.I)
_NEXT_LETTER_ITEM = re.compile(r"\([m-z]\)[ \t]+", re.I)


class SourcePdfError(Exception):
    """The registered original cannot be safely read."""


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    tessdata_dir: str | None = None
    languages: str = "eng"
    dpi: int = 200
    min_native_chars: int = 20
    mixed_image_area_ratio: float = 0.20
    review_manifest: dict | None = None

    def digest(self) -> str:
        # The tessdata location is deployment-specific; its contents/version
        # belong in deployment records, not the text-algorithm identity.
        payload = {
            "extractor": EXTRACTOR_VERSION,
            "pymupdf": fitz.VersionBind,
            "ocr_languages": self.languages,
            "ocr_dpi": self.dpi,
            "min_native_chars": self.min_native_chars,
            "mixed_image_area_ratio": self.mixed_image_area_ratio,
            "review_manifest": self.review_manifest,
        }
        return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    ordinal: int
    kind: str
    raw_text: str
    normalized_text: str
    page_char_start: int | None
    page_char_end: int | None
    bbox: dict[str, float]
    warnings: list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_index: int
    width_pt: float | None
    height_pt: float | None
    rotation_deg: int | None
    raw_text: str
    normalized_text: str
    extraction_method: str
    outcome: str
    warnings: list[dict[str, str]]
    blocks: list[ExtractedBlock]
    ocr_confidence: None = None
    table_regions: list[dict] | None = None

    def digest_payload(self) -> bytes:
        value = {
            "page_index": self.page_index,
            "text": self.normalized_text,
            "method": self.extraction_method,
            "outcome": self.outcome,
            "warnings": self.warnings,
            "blocks": [
                {"kind": block.kind, "text": block.normalized_text, "bbox": block.bbox}
                for block in self.blocks
            ],
            "table_regions": self.table_regions or [],
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def warning(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def ambiguous_one_candidates(value: str) -> list[int]:
    """Flag likely text-layer l/I versus printed 1; never auto-correct law."""
    positions = []
    for marker in _AMBIGUOUS_MARKER.finditer(value):
        before = value[max(0, marker.start() - 800):marker.start()]
        after = value[marker.end():marker.end() + 800]
        # An alphabetic list that reaches (l) is not a subsection (1).
        if _PREVIOUS_LETTER_ITEM.search(before) and _NEXT_LETTER_ITEM.search(after):
            continue
        if _NUMBERED_HEADING.search(before) or _SECOND_SUBSECTION.search(after):
            positions.append(marker.start())
    return positions


def resolve_source_pdf(upload_dir: str, object_key: str, expected_sha256: str) -> Path:
    """Never let a database value escape the private legal upload directory."""
    if not _OBJECT_KEY.fullmatch(object_key) or object_key != (
        f"{expected_sha256[:2]}/{expected_sha256}.pdf"
    ):
        raise SourcePdfError("The registered PDF storage key is invalid.")
    root = Path(upload_dir).resolve()
    path = (root / object_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise SourcePdfError("The registered PDF is missing from local storage.")
    return path


def verify_source_pdf(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourcePdfError("The registered PDF could not be read.") from exc
    if digest.hexdigest() != expected_sha256:
        raise SourcePdfError("The registered PDF no longer matches its SHA-256 hash.")


def _safe_text(value: str) -> str:
    # PostgreSQL text cannot contain NUL. Keep the replacement visible for
    # inspection instead of silently dropping source characters.
    return value.replace("\x00", "\ufffd")


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", _safe_text(value).replace("\r\n", "\n").replace("\r", "\n"))


def _bbox(raw: tuple[Any, ...]) -> dict[str, float]:
    return dict(zip(("x0", "y0", "x1", "y1"), (round(float(x), 3) for x in raw[:4]), strict=True))


def _build_blocks(items: list[tuple[Any, ...]]) -> tuple[str, str, list[ExtractedBlock]]:
    blocks: list[ExtractedBlock] = []
    raw_parts: list[str] = []
    normalized_parts: list[str] = []
    cursor = 0
    for item in items:
        kind = "paragraph" if item[6] == 0 else "figure"
        raw = _safe_text(str(item[4])) if kind == "paragraph" else ""
        normalized = _normalize(raw)
        if kind == "paragraph" and not normalized.strip():
            continue
        start: int | None = None
        end: int | None = None
        if kind == "paragraph":
            if normalized_parts:
                cursor += 1  # the deterministic separator between blocks
            start = cursor
            cursor += len(normalized)
            end = cursor
            raw_parts.append(raw)
            normalized_parts.append(normalized)
        blocks.append(ExtractedBlock(
            ordinal=len(blocks) + 1,
            kind=kind,
            raw_text=raw,
            normalized_text=normalized,
            page_char_start=start,
            page_char_end=end,
            bbox=_bbox(item),
            warnings=[warning("image_region", "Image content needs visual inspection.", "info")]
            if kind == "figure" else [],
        ))
    return "\n".join(raw_parts), "\n".join(normalized_parts), blocks


def _table_regions(page: fitz.Page) -> list[dict]:
    """Keep table geometry separate from the exact native-text block stream.

    A detected cell is a layout aid, not a corrected legal transcription. The
    table finder may merge printed subrows, so every region requires review.
    """
    regions: list[dict] = []
    for table in page.find_tables().tables:
        if table.row_count < 2 or table.col_count < 2:
            continue
        extracted = table.extract()
        rows = []
        for row_index in range(table.row_count):
            row = []
            for column_index in range(table.col_count):
                table_row = table.rows[row_index] if row_index < len(table.rows) else None
                cell_bbox = (table_row.cells[column_index]
                             if table_row is not None and column_index < len(table_row.cells)
                             else None)
                cell_text = (extracted[row_index][column_index]
                             if row_index < len(extracted)
                             and column_index < len(extracted[row_index]) else None)
                row.append({
                    "text": _normalize(cell_text) if cell_text is not None else None,
                    "bbox": _bbox(cell_bbox) if cell_bbox is not None else None,
                })
            rows.append(row)
        region = {
            "bbox": _bbox(table.bbox), "row_count": table.row_count,
            "column_count": table.col_count, "rows": rows,
            "status": "candidate_requires_review",
        }
        try:
            region["atomic_rows"], _ = atomize_table_region(region)
        except ValueError as exc:
            region["atomic_rows"] = []
            region["atomization_error"] = str(exc)
        regions.append(region)
    return regions


def apply_reviewed_edits(page: ExtractedPage, edits: dict, *, reviewer: str) -> ExtractedPage:
    """Build a new extraction result; preserve raw text and earlier attempts."""
    if not reviewer.strip():
        raise ValueError("Reviewed extraction must identify its reviewer.")
    if page.outcome == "failed":
        raise ValueError("A failed page cannot be accepted by a correction manifest.")
    blocks = list(page.blocks)
    warnings = list(page.warnings)
    regions = deepcopy(page.table_regions or [])
    for edit in edits.get("text_replacements", []):
        ordinal = edit["block_ordinal"]
        expected = edit["original"]
        replacement = edit["replacement"]
        if not expected or len(expected) != len(replacement):
            raise ValueError("Reviewed glyph replacement must be nonempty and length-preserving.")
        if not 1 <= ordinal <= len(blocks):
            raise ValueError("Reviewed correction targets a missing source block.")
        source = blocks[ordinal - 1]
        if source.kind != "paragraph" or source.normalized_text.count(expected) != 1:
            raise ValueError("Reviewed correction must match one exact text-layer occurrence.")
        blocks[ordinal - 1] = replace(
            source, normalized_text=source.normalized_text.replace(expected, replacement, 1),
            warnings=[*source.warnings, warning(
                "visually_corrected_glyph",
                f"{reviewer}: text layer {expected!r} was visually confirmed as {replacement!r}.",
                "info",
            )],
        )
        warnings.append(warning(
            "visually_corrected_glyph",
            f"Block {ordinal}: text-layer {expected!r} was corrected to visually printed {replacement!r} by {reviewer}; raw text is retained.",
            "info",
        ))
    for review in edits.get("review_tables", []):
        index = review["table_index"]
        if not 0 <= index < len(regions):
            raise ValueError("Reviewed table index is not present on this PDF page.")
        region = regions[index]
        atomic_rows, _ = atomize_table_region(
            region, inherited_enactment=review.get("inherited_enactment"),
        )
        if not atomic_rows or not any(row["row_type"] == "entry" for row in atomic_rows):
            raise ValueError("Reviewed table has no atomic entry rows.")
        region["atomic_rows"] = atomic_rows
        region.pop("atomization_error", None)
        region["status"] = "visually_reviewed_rows"
        region["reviewed_by"] = reviewer
        region["review_note"] = review.get("reason", "Compared every row with the original PDF.")
        if not reviewed_region_is_lossless(region):
            raise ValueError("Reviewed table rows do not cover the saved source cells exactly.")
    if regions and all(region["status"] == "visually_reviewed_rows" for region in regions):
        warnings = [item for item in warnings if item["code"] != "table_layout_candidate"]
        warnings.append(warning(
            "table_rows_visually_reviewed",
            f"All detected table row groups and atomic entries were visually reviewed by {reviewer}.",
            "info",
        ))
    normalized_text = "\n".join(
        block.normalized_text for block in blocks if block.kind == "paragraph"
    )
    candidates = ambiguous_one_candidates(normalized_text)
    candidate_block_by_position = {
        position: next((block.ordinal for block in blocks
                        if block.page_char_start is not None
                        and block.page_char_start <= position < block.page_char_end), None)
        for position in candidates
    }
    candidate_blocks = set(candidate_block_by_position.values()) - {None}
    confirmed_blocks = set(edits.get("confirm_numbering_glyph_blocks", []))
    if (any(not isinstance(ordinal, int) for ordinal in confirmed_blocks)
            or not confirmed_blocks.issubset(candidate_blocks)):
        raise ValueError("A confirmed numbering-glyph block must contain a current ambiguous candidate.")
    if confirmed_blocks:
        warnings.append(warning(
            "numbering_glyph_confirmed_as_printed",
            f"{reviewer} visually confirmed the text layer in blocks {sorted(confirmed_blocks)}.",
            "info",
        ))
    if not candidates or (None not in candidate_block_by_position.values()
                          and candidate_blocks == confirmed_blocks):
        warnings = [item for item in warnings if item["code"] != "ambiguous_numbering_glyph"]
    elif not any(item["code"] == "ambiguous_numbering_glyph" for item in warnings):
        warnings.append(warning("ambiguous_numbering_glyph",
                                "A possible subsection (1) still needs visual confirmation."))
    if len(normalized_text) != len(page.normalized_text):
        raise ValueError("Reviewed correction changed page offsets; rebuild is unsafe.")
    for block in blocks:
        if block.page_char_start is not None and normalized_text[
            block.page_char_start:block.page_char_end
        ] != block.normalized_text:
            raise ValueError("Reviewed correction broke the page-to-block source offsets.")
    unresolved = any(item.get("severity") != "info" for item in warnings)
    outcome = "needs_review" if unresolved else "extracted"
    return replace(page, normalized_text=normalized_text, blocks=blocks,
                   table_regions=regions, warnings=warnings, outcome=outcome)


def _tessdata(config: ExtractionConfig) -> str:
    languages = [part for part in config.languages.split("+") if part]
    if not languages:
        raise RuntimeError("No OCR language is configured.")
    folder = Path(config.tessdata_dir).resolve() if config.tessdata_dir else Path(fitz.get_tessdata())
    missing = [language for language in languages if not (folder / f"{language}.traineddata").is_file()]
    if missing:
        raise RuntimeError("Missing Tesseract language data: " + ", ".join(missing))
    return str(folder)


def extract_page(page: fitz.Page, page_index: int, config: ExtractionConfig) -> ExtractedPage:
    """Extract one physical page without assuming it contains legal sections."""
    # The default BLOCKS flags omit images. Preserve their positioned metadata
    # so a scan cannot be mistaken for a truly blank page.
    native_items = page.get_text(
        "blocks", flags=fitz.TEXTFLAGS_BLOCKS | fitz.TEXT_PRESERVE_IMAGES, sort=True
    )
    native_text = "".join(str(item[4]) for item in native_items if item[6] == 0)
    visible_chars = len(native_text.strip())
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    image_area = max((
        max(0.0, float(item[2] - item[0])) * max(0.0, float(item[3] - item[1])) / page_area
        for item in native_items if item[6] == 1
    ), default=0.0)
    drawings = len(page.get_drawings()) if visible_chars < config.min_native_chars else 0
    replacement_ratio = native_text.count("\ufffd") / max(len(native_text), 1)
    has_visual_content = image_area > 0 or drawings > 0
    needs_ocr = (
        image_area >= config.mixed_image_area_ratio
        or (visible_chars < config.min_native_chars and has_visual_content)
        or replacement_ratio > 0.02
    )

    items = native_items
    method = "native" if visible_chars else "none"
    outcome = "extracted" if visible_chars else "needs_review"
    warnings: list[dict[str, str]] = []

    if needs_ocr:
        try:
            tessdata = _tessdata(config)
            full = not visible_chars or replacement_ratio > 0.02
            textpage = page.get_textpage_ocr(
                language=config.languages, dpi=config.dpi, full=full, tessdata=tessdata
            )
            ocr_items = page.get_text("blocks", textpage=textpage, sort=True)
            # OCR's TextPage may omit the original image block. Keep the source
            # image position visible in the inspector as a separate figure.
            if not any(item[6] == 1 for item in ocr_items):
                ocr_items += [item for item in native_items if item[6] == 1]
                ocr_items.sort(key=lambda item: (item[3], item[0]))
            ocr_text = "".join(str(item[4]) for item in ocr_items if item[6] == 0)
            if ocr_text.strip():
                items = ocr_items
                method = "mixed" if visible_chars and not full else "ocr"
                outcome = "needs_review"
                warnings.append(warning("ocr_unverified", "OCR text must be checked against the original page."))
                if method == "mixed":
                    warnings.append(warning("mixed_reading_order", "Native and OCR text order may not match visual reading order."))
            else:
                outcome = "needs_review" if visible_chars else "failed"
                warnings.append(warning("ocr_no_text", "OCR returned no text from visual content.", "error"))
        except Exception as exc:  # OCR dependencies and corrupt page errors vary by platform.
            outcome = "needs_review" if visible_chars else "failed"
            warnings.append(warning("ocr_failed", f"OCR could not run: {exc}", "error"))
    elif not visible_chars:
        warnings.append(warning("blank_candidate", "No text or image content was detected; a person must verify this page is blank."))
    elif visible_chars < config.min_native_chars:
        outcome = "needs_review"
        warnings.append(warning("low_text", "Very little selectable text was found; inspect the original page."))

    if replacement_ratio > 0.02:
        warnings.append(warning("broken_text_encoding", "Native text contains many replacement characters."))
    if "\x00" in native_text:
        warnings.append(warning("nul_replaced", "NUL characters were replaced for database storage."))
    if ambiguous_one_candidates(native_text):
        warnings.append(warning(
            "ambiguous_numbering_glyph",
            "Native text may read letter l/I where subsection (1) is printed; compare the PDF image. Never auto-correct a legal reference.",
        ))
        outcome = "needs_review"

    try:
        table_regions = _table_regions(page)
    except Exception as exc:
        # An undetected table would be stored as flat text and skip table
        # review, so a detector failure sends the page to a person.
        table_regions = []
        warnings.append(warning("table_detection_failed", f"Table layout detection could not run: {exc}"))
        if outcome == "extracted":
            outcome = "needs_review"
    if table_regions:
        warnings.append(warning("table_layout_candidate", "Detected table rows and columns require visual review."))
        outcome = "needs_review"

    raw_text, normalized_text, blocks = _build_blocks(items)
    if not normalized_text.strip() and outcome == "extracted":
        outcome = "needs_review"
        warnings.append(warning("empty_blocks", "Text extraction produced no usable text blocks."))

    return ExtractedPage(
        page_index=page_index,
        width_pt=round(float(page.rect.width), 3),
        height_pt=round(float(page.rect.height), 3),
        rotation_deg=int(page.rotation),
        raw_text=raw_text,
        normalized_text=normalized_text,
        extraction_method=method,
        outcome=outcome,
        warnings=warnings,
        blocks=blocks,
        table_regions=table_regions,
    )
