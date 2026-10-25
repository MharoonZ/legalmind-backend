"""Page extraction and original-page rendering, isolated from the HTTP process.

No legal parsing. Normalization changes CRLF/CR to LF and removes trailing
spaces/tabs per line only; it preserves spelling, punctuation and line breaks.
PyMuPDF work runs in child processes because its objects are not thread-safe.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

import fitz

NORMALIZER_VERSION = "line-endings-trailing-space-v1"
EXTRACTOR_VERSION = "physical-pages-v1"


class PdfProblem(Exception):
    """Safe diagnostic code; no filesystem paths or provider secrets."""


def normalize_text(text: str) -> str:
    return "\n".join(line.rstrip(" \t") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def content_hash(value) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def warning(code: str, message: str, **details) -> dict:
    return {"code": code, "message": message, **details}


def resolve_pdf(root: str, object_key: str, sha256: str) -> Path:
    # Registration uses this exact content-addressed format. Neither callers
    # nor a corrupted DB object_key may point at another file or drive.
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise PdfProblem("invalid_pdf_hash")
    expected = f"{sha256[:2]}/{sha256}.pdf"
    if object_key != expected or PurePosixPath(object_key).is_absolute():
        raise PdfProblem("unsafe_pdf_location")
    root_path = Path(root).resolve()
    path = (root_path / object_key).resolve()
    if not path.is_relative_to(root_path):
        raise PdfProblem("unsafe_pdf_location")
    if not path.is_file():
        raise PdfProblem("source_pdf_missing")
    return path


def verified_bytes(source: dict) -> bytes:
    path = resolve_pdf(source["root"], source["object_key"], source["sha256"])
    try:
        if path.stat().st_size != source["byte_size"]:
            raise PdfProblem("source_size_mismatch")
        data = path.read_bytes()
    except OSError as exc:
        raise PdfProblem("source_pdf_unreadable") from exc
    if len(data) != source["byte_size"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise PdfProblem("source_hash_mismatch")
    return data


def open_pdf(source: dict):
    data = verified_bytes(source)  # Open the very bytes that were verified.
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
        if pdf.is_encrypted or pdf.needs_pass or pdf.page_count < 1:
            pdf.close()
            raise PdfProblem("pdf_encrypted_or_empty")
        return pdf
    except PdfProblem:
        raise
    except Exception as exc:
        raise PdfProblem("pdf_unreadable") from exc


def ocr_prerequisites(config: dict) -> dict:
    result = {"available": False, "engine_version": None, "languages": [], "problem": None}
    if not config["ocr_enabled"]:
        return {**result, "problem": "ocr_disabled"}
    executable = shutil.which(config["tesseract_path"])
    if not executable:
        return {**result, "problem": "tesseract_missing"}
    args = [executable]
    if config["tessdata_dir"]:
        args += ["--tessdata-dir", config["tessdata_dir"]]
    try:
        options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 10}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        version = subprocess.run([executable, "--version"], **options)
        languages = subprocess.run([*args, "--list-langs"], **options)
        if version.returncode or languages.returncode:
            return {**result, "problem": "tesseract_configuration_failed"}
        found = [line.strip() for line in languages.stdout.splitlines()[1:] if line.strip()]
        required = config["languages"].split("+")
        missing = [lang for lang in required if lang not in found]
        return {"available": not missing, "engine_version": version.stdout.splitlines()[0][:100],
                "languages": found, "problem": "ocr_language_missing" if missing else None}
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "problem": "tesseract_configuration_failed"}


def extraction_config(settings) -> dict:
    config = {"engine": "PyMuPDF", "engine_version": fitz.VersionBind,
              "extractor_version": EXTRACTOR_VERSION, "normalizer": NORMALIZER_VERSION,
              "ocr_enabled": settings.legal_ocr_enabled, "tesseract_path": settings.legal_tesseract_path,
              "tessdata_dir": settings.legal_tessdata_dir, "languages": settings.legal_ocr_languages,
              "dpi": settings.legal_ocr_dpi, "min_native_chars": settings.legal_native_min_chars,
              "page_timeout": settings.legal_page_timeout}
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", config["languages"]):
        raise PdfProblem("invalid_ocr_languages")
    config["ocr"] = ocr_prerequisites(config)
    return config


def _box(rect) -> dict:
    return {"x0": round(rect.x0, 3), "y0": round(rect.y0, 3),
            "x1": round(rect.x1, 3), "y1": round(rect.y1, 3),
            "space": "rotated_page_points"}


def _overlap(box, native) -> bool:
    a, b = fitz.Rect(box), fitz.Rect(native)
    return a.get_area() > 0 and (a & b).get_area() / a.get_area() > 0.5


def tsv_blocks(tsv: str, scale_x: float, scale_y: float, native_boxes=()) -> list[dict]:
    """Retain OCR words outside reliable native text, group in OCR line order.

    No inferred confidence: page confidence stays NULL. Raw measured per-word
    confidence is deliberately not aggregated into a misleading page score.
    """
    lines: dict[tuple, list] = {}
    # Tesseract TSV contains literal quotation marks, not CSV-quoted fields.
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        try:
            x, y, width, height = (float(row[k]) for k in ("left", "top", "width", "height"))
            if not all(math.isfinite(n) for n in (x, y, width, height)) or width <= 0 or height <= 0:
                raise ValueError("Invalid OCR bounding box")
            rect = fitz.Rect(x * scale_x, y * scale_y, (x + width) * scale_x, (y + height) * scale_y)
        except (ValueError, KeyError) as exc:
            raise PdfProblem("ocr_invalid_output") from exc
        if any(_overlap(rect, native) for native in native_boxes):
            continue
        key = tuple(row.get(k, "") for k in ("block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append((row["text"], rect))
    output = []
    for words in lines.values():
        rect = fitz.Rect(words[0][1])
        for _, word_rect in words[1:]:
            rect |= word_rect
        output.append({"raw_text": " ".join(word for word, _ in words), "bbox": _box(rect),
                       "block_kind": "other", "warnings": [warning("ocr_text", "OCR text requires comparison with the original page.")]})
    return output


def recognize(page, config: dict, native_boxes=()) -> list[dict]:
    if not config["ocr"]["available"]:
        raise PdfProblem(config["ocr"]["problem"] or "ocr_unavailable")
    # Bounded raster size also protects the local inspector from giant pages.
    if page.rect.width * page.rect.height * (config["dpi"] / 72) ** 2 > 40_000_000:
        raise PdfProblem("ocr_page_too_large")
    with tempfile.TemporaryDirectory(prefix="legal-ocr-") as folder:
        image = Path(folder) / "page.png"
        pix = page.get_pixmap(dpi=config["dpi"], alpha=False, colorspace=fitz.csRGB)
        pix.save(str(image))
        args = [config["tesseract_path"], str(image), "stdout", "-l", config["languages"], "--dpi", str(config["dpi"])]
        if config["tessdata_dir"]:
            args += ["--tessdata-dir", config["tessdata_dir"]]
        args += ["tsv"]
        options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
                   "timeout": max(1, config["page_timeout"] - 5)}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(args, **options)
        except subprocess.TimeoutExpired as exc:
            raise PdfProblem("ocr_timeout") from exc
        except OSError as exc:
            raise PdfProblem("ocr_unavailable") from exc
        if result.returncode:
            raise PdfProblem("ocr_failed")
        if not result.stdout.startswith("level\t"):
            raise PdfProblem("ocr_invalid_output")
        return tsv_blocks(result.stdout, page.rect.width / pix.width, page.rect.height / pix.height, native_boxes)


def page_result(page, config: dict) -> dict:
    warnings = []
    blocks = []
    image_area = 0.0
    for block in page.get_text("blocks", sort=True, flags=fitz.TEXTFLAGS_BLOCKS | fitz.TEXT_PRESERVE_IMAGES):
        rect = fitz.Rect(block[:4]) * page.rotation_matrix
        if block[6] == 0:
            blocks.append({"raw_text": block[4], "bbox": _box(rect), "block_kind": "other", "warnings": []})
        else:
            image_area += rect.get_area()
            blocks.append({"raw_text": "", "bbox": _box(rect), "block_kind": "figure",
                           "warnings": [warning("image_region", "Image region; inspect for text, stamps or diagrams.")]})
    native = "\n\n".join(b["raw_text"] for b in blocks if b["raw_text"])
    chars = sum(c.isalnum() for c in native)
    corrupt = sum(c in "\ufffd\x00" or (ord(c) < 32 and c not in "\n\r\t") for c in native)
    suspect = corrupt > 0 and corrupt / max(1, len(native)) >= 0.01
    sparse = chars < config["min_native_chars"]
    mixed = bool(native.strip()) and image_area / max(1, page.rect.get_area()) >= 0.2
    method = "native" if native.strip() else "none"
    outcome = "extracted"
    if sparse:
        warnings.append(warning("sparse_native_text", "Native text is absent or suspiciously sparse.", native_characters=chars))
    if suspect:
        warnings.append(warning("corrupt_native_text", "Native text contains replacement or control characters."))
    if mixed:
        warnings.append(warning("mixed_page", "Substantial image content appears alongside native text."))
    if sparse or suspect or mixed:
        reliable_native = bool(native.strip()) and not suspect
        native_boxes = [fitz.Rect([b["bbox"][k] for k in ("x0", "y0", "x1", "y1")])
                        for b in blocks if b["raw_text"].strip()] if reliable_native else []
        try:
            recognized = recognize(page, config, native_boxes)
            if recognized:
                if reliable_native:
                    blocks.extend(recognized)
                    method = "mixed"
                else:
                    blocks = [b for b in blocks if b["block_kind"] == "figure"] + recognized
                    method = "ocr"
                warnings.append(warning("ocr_review_required", "OCR is a machine transcription, not verified legal text."))
            else:
                warnings.append(warning("ocr_no_additional_text", "OCR found no additional text; this does not establish that the page is blank."))
            outcome = "needs_review"
        except PdfProblem as exc:
            warnings.append(warning(str(exc), "Required OCR could not be completed. Inspect the original and resolve the OCR prerequisite."))
            outcome = "needs_review" if native.strip() else "failed"
    blocks.sort(key=lambda b: (b["bbox"]["y0"], b["bbox"]["x0"]))
    normalized, raw = "", ""
    for ordinal, block in enumerate(blocks, 1):
        block["block_ordinal"] = ordinal
        block["normalized_text"] = normalize_text(block["raw_text"])
        block["text_sha256"] = hashlib.sha256(block["normalized_text"].encode()).hexdigest()
        block["page_char_start"] = block["page_char_end"] = None
        if block["normalized_text"]:
            if normalized:
                normalized += "\n\n"
            block["page_char_start"] = len(normalized)
            normalized += block["normalized_text"]
            block["page_char_end"] = len(normalized)
        if block["raw_text"]:
            raw += ("\n\n" if raw else "") + block["raw_text"]
    if not normalized.strip():
        warnings.append(warning("no_text_unverified", "No text was extracted. A human must decide whether this page is blank."))
        if outcome == "extracted":
            outcome = "needs_review"
    return {"page_index": page.number + 1, "width_pt": page.rect.width, "height_pt": page.rect.height,
            "rotation_deg": page.rotation, "printed_label": page.get_label() or None,
            "raw_text": raw, "normalized_text": normalized,
            "text_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            "extraction_method": method, "outcome": outcome, "ocr_confidence": None,
            "warnings": warnings, "blocks": blocks}


def failed_page(index: int, code: str) -> dict:
    return {"page_index": index, "width_pt": None, "height_pt": None, "rotation_deg": None,
            "printed_label": None, "raw_text": "", "normalized_text": "",
            "text_sha256": hashlib.sha256(b"").hexdigest(), "extraction_method": "none",
            "outcome": "failed", "ocr_confidence": None,
            "warnings": [warning(code, "Page processing failed; the original still requires human inspection.")], "blocks": []}


def execute(payload: dict) -> dict:
    with open_pdf(payload["source"]) as pdf:
        if payload["operation"] == "inspect":
            return {"page_count": pdf.page_count, "engine_version": fitz.VersionBind}
        index = payload["page_index"]
        if index < 1 or index > pdf.page_count:
            raise PdfProblem("page_out_of_range")
        page = pdf[index - 1]
        if payload["operation"] == "preview":
            dpi = payload["dpi"]
            if page.rect.get_area() * (dpi / 72) ** 2 > 20_000_000:
                raise PdfProblem("preview_page_too_large")
            png = page.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB).tobytes("png")
            return {"png": base64.b64encode(png).decode("ascii")}
        return page_result(page, payload["config"])


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.buffer.read(64_000))
        result = {"ok": True, "result": execute(payload)}
    except PdfProblem as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception:
        result = {"ok": False, "error": "page_processing_failed"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
