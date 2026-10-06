"""Page-faithful PDF extraction for the reviewed legal corpus.

No section parsing happens here. Every physical page receives an outcome, and
OCR output is deliberately marked for human review rather than trusted as law.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz


EXTRACTOR_VERSION = "legal-pages-v1"
_OBJECT_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.pdf$")


class SourcePdfError(Exception):
    """The registered original cannot be safely read."""


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    tessdata_dir: str | None = None
    languages: str = "eng"
    dpi: int = 200
    min_native_chars: int = 20
    mixed_image_area_ratio: float = 0.20

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
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def warning(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


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
    )
