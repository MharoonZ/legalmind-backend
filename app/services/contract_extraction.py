"""Extract uploaded service agreements into versioned pages and clauses.

Docling supplies reading order, headings, tables, and page provenance. PyMuPDF
supplies a canonical selectable-text copy of every page. Keeping those roles
separate makes the extraction output auditable and keeps clause segmentation
deterministic enough to test without loading Docling's ML models.

Uploaded document text is data only. It is never evaluated as an instruction
and this module makes no LLM calls.
"""

from __future__ import annotations

import asyncio

import hashlib
import importlib.metadata
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import fitz  # PyMuPDF

from app.core.errors import ContractExtractionError

logger = logging.getLogger(__name__)

_CLAUSE_JOINER = "\n\n"
_NUMBERED_CLAUSE_RE = re.compile(
    r"^\s*(?P<label>(?:\d+(?:\.\d+)*(?:[.)])?|[A-Za-z][.)]|"
    r"\([A-Za-z0-9ivxlcdmIVXLCDM]+\)|[ivxlcdmIVXLCDM]+[.)]))"
    r"\s+(?P<heading>[^\n]{1,240})"
)
_STRUCTURAL_LABELS = {"title", "section_header"}
_IGNORED_LABELS = {"page_header", "page_footer", "picture"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractExtractionError("The stored contract file could not be read.") from exc
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    text: str
    text_sha256: str


@dataclass(frozen=True, slots=True)
class StructuredBlock:
    """One body item in reading order, with one or more source pages."""

    kind: str
    text: str
    page_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ClausePageSpan:
    """The clause characters contributed by a block found on one PDF page."""

    page_number: int
    clause_char_start: int
    clause_char_end: int


@dataclass(frozen=True, slots=True)
class ExtractedClause:
    clause_id: str
    ordinal: int
    label: str | None
    heading: str | None
    text: str
    text_sha256: str
    page_start: int
    page_end: int
    spans: tuple[ClausePageSpan, ...]


@dataclass(frozen=True, slots=True)
class ExtractedContract:
    document_version_id: str
    engine_name: str
    engine_version: str
    extracted_text_sha256: str
    pages: tuple[ExtractedPage, ...]
    clauses: tuple[ExtractedClause, ...]


@dataclass(frozen=True, slots=True)
class ExtractionSource:
    document_id: str
    version_id: str
    storage_path: str
    pdf_sha256: str
    page_count: int
    status: str


class StructuredExtractionEngine(Protocol):
    name: str
    version: str

    def extract_blocks(self, pdf_path: Path) -> list[StructuredBlock]: ...


class ExtractionRepository(Protocol):
    async def get_extraction_source(self, version_id: str) -> ExtractionSource | None: ...

    async def mark_extracting(self, version_id: str) -> None: ...

    async def replace_extraction(self, extraction: ExtractedContract) -> None: ...

    async def mark_extraction_failed(self, version_id: str, message: str) -> None: ...


def _label_value(item: object) -> str:
    label = getattr(item, "label", "")
    return str(getattr(label, "value", label)).lower()


def _body_item(item: object) -> bool:
    layer = getattr(item, "content_layer", "body")
    return str(getattr(layer, "value", layer)).lower() == "body"


def _page_numbers(item: object, previous_page: int) -> tuple[int, ...]:
    pages = sorted(
        {
            int(page)
            for provenance in (getattr(item, "prov", None) or ())
            if (page := getattr(provenance, "page_no", None)) is not None
        }
    )
    # PDF items normally have provenance. Falling back to the previous page is
    # safer than dropping an otherwise valid title or list marker.
    return tuple(pages or [previous_page])


def _item_text(item: object, document: object, kind: str) -> str:
    if kind == "table":
        exporter = getattr(item, "export_to_markdown", None)
        if callable(exporter):
            try:
                return str(exporter(doc=document)).strip()
            except TypeError:
                return str(exporter(document)).strip()
    return str(getattr(item, "text", "")).strip()


class DoclingExtractionEngine:
    """Docling adapter configured for digital PDFs only (OCR stays disabled)."""

    name = "docling"

    def __init__(self, *, max_pages: int = 10, max_file_size: int = 10 * 1024 * 1024):
        self.max_pages = max_pages
        self.max_file_size = max_file_size
        try:
            self.version = importlib.metadata.version("docling")
        except importlib.metadata.PackageNotFoundError:
            self.version = "not-installed"

    def extract_blocks(self, pdf_path: Path) -> list[StructuredBlock]:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise ContractExtractionError(
                "Docling is not installed. Install the backend requirements before extraction."
            ) from exc

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        pipeline_options.do_table_structure = True
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            },
        )

        try:
            result = converter.convert(
                pdf_path,
                max_num_pages=self.max_pages,
                max_file_size=self.max_file_size,
            )
        except Exception as exc:  # Docling exposes several pipeline exceptions
            raise ContractExtractionError("Docling could not extract this contract.") from exc

        document = result.document
        blocks: list[StructuredBlock] = []
        previous_page = 1
        for item, _level in document.iterate_items():
            if not _body_item(item):
                continue
            kind = _label_value(item)
            if kind in _IGNORED_LABELS:
                continue
            text = _item_text(item, document, kind)
            if not text:
                continue
            pages = _page_numbers(item, previous_page)
            previous_page = pages[-1]
            blocks.append(StructuredBlock(kind=kind, text=text, page_numbers=pages))

        if not blocks:
            raise ContractExtractionError("Docling returned no selectable contract text.")
        return blocks


def _read_pages(pdf_path: Path) -> tuple[ExtractedPage, ...]:
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # PyMuPDF uses several exception subclasses
        raise ContractExtractionError("The stored contract is not a readable PDF.") from exc

    try:
        if pdf.is_encrypted:
            raise ContractExtractionError("Encrypted contracts cannot be extracted.")
        pages = []
        for index, page in enumerate(pdf, start=1):
            text = page.get_text("text", sort=True).strip()
            pages.append(
                ExtractedPage(
                    page_number=index,
                    text=text,
                    text_sha256=_sha256_text(text),
                )
            )
        if not pages:
            raise ContractExtractionError("The stored contract has no pages.")
        return tuple(pages)
    finally:
        pdf.close()


def _boundary(block: StructuredBlock) -> tuple[str | None, str | None] | None:
    first_line = block.text.splitlines()[0].strip()
    match = _NUMBERED_CLAUSE_RE.match(first_line)
    if match:
        return match.group("label"), match.group("heading").strip()
    if block.kind in _STRUCTURAL_LABELS:
        return None, first_line[:240]
    return None


def _clause_id(version_id: str, ordinal: int, label: str | None, text_sha256: str) -> str:
    raw = f"{version_id}\0{ordinal}\0{label or ''}\0{text_sha256}".encode("utf-8")
    return "cl:" + hashlib.sha256(raw).hexdigest()[:32]


def _build_clause(
    *,
    version_id: str,
    ordinal: int,
    label: str | None,
    heading: str | None,
    blocks: list[StructuredBlock],
) -> ExtractedClause:
    parts: list[str] = []
    spans: list[ClausePageSpan] = []
    length = 0
    for block in blocks:
        if parts:
            parts.append(_CLAUSE_JOINER)
            length += len(_CLAUSE_JOINER)
        start = length
        parts.append(block.text)
        length += len(block.text)
        for page_number in block.page_numbers:
            spans.append(
                ClausePageSpan(
                    page_number=page_number,
                    clause_char_start=start,
                    clause_char_end=length,
                )
            )

    text = "".join(parts).strip()
    if not text or not spans:
        raise ContractExtractionError("A clause was produced without text or page provenance.")
    text_sha256 = _sha256_text(text)
    page_numbers = [span.page_number for span in spans]
    return ExtractedClause(
        clause_id=_clause_id(version_id, ordinal, label, text_sha256),
        ordinal=ordinal,
        label=label,
        heading=heading,
        text=text,
        text_sha256=text_sha256,
        page_start=min(page_numbers),
        page_end=max(page_numbers),
        spans=tuple(spans),
    )


def segment_clauses(
    blocks: list[StructuredBlock], *, version_id: str
) -> tuple[ExtractedClause, ...]:
    """Group Docling body items into stable clauses in reading order."""
    if not blocks:
        raise ContractExtractionError("No structured blocks are available for segmentation.")

    clauses: list[ExtractedClause] = []
    current: list[StructuredBlock] = []
    current_label: str | None = None
    current_heading: str | None = None

    def finish() -> None:
        nonlocal current, current_label, current_heading
        if not current:
            return
        clauses.append(
            _build_clause(
                version_id=version_id,
                ordinal=len(clauses) + 1,
                label=current_label,
                heading=current_heading,
                blocks=current,
            )
        )
        current = []
        current_label = None
        current_heading = None

    for block in blocks:
        marker = _boundary(block)
        if marker is not None and current:
            finish()
        if not current and marker is not None:
            current_label, current_heading = marker
        current.append(block)
    finish()

    if not clauses:
        raise ContractExtractionError("No clauses could be produced from the contract.")
    return tuple(clauses)


def extract_contract(
    pdf_path: str | Path,
    *,
    document_version_id: str,
    engine: StructuredExtractionEngine,
) -> ExtractedContract:
    """Pure extraction entry point used by the service, CLI, and unit tests."""
    path = Path(pdf_path)
    if not path.is_file():
        raise ContractExtractionError("The stored contract file does not exist.")

    pages = _read_pages(path)
    blocks = engine.extract_blocks(path)
    page_count = len(pages)
    bad_pages = sorted(
        {
            page
            for block in blocks
            for page in block.page_numbers
            if page < 1 or page > page_count
        }
    )
    if bad_pages:
        raise ContractExtractionError(
            f"Extractor returned page numbers outside the PDF: {bad_pages}."
        )

    clauses = segment_clauses(blocks, version_id=document_version_id)
    combined = _CLAUSE_JOINER.join(clause.text for clause in clauses)
    return ExtractedContract(
        document_version_id=document_version_id,
        engine_name=engine.name,
        engine_version=engine.version,
        extracted_text_sha256=_sha256_text(combined),
        pages=pages,
        clauses=clauses,
    )


class ContractExtractionService:
    """Orchestrate one extraction while keeping status transitions truthful."""

    def __init__(
        self,
        repository: ExtractionRepository,
        engine: StructuredExtractionEngine,
    ) -> None:
        self.repository = repository
        self.engine = engine

    async def extract(self, version_id: str) -> ExtractedContract:
        source = await self.repository.get_extraction_source(version_id)
        if source is None:
            raise ContractExtractionError("Document version was not found.")

        path = Path(source.storage_path)
        await self.repository.mark_extracting(version_id)
        try:
            if await asyncio.to_thread(_sha256_file, path) != source.pdf_sha256:
                raise ContractExtractionError(
                    "Stored contract hash does not match the uploaded file record."
                )
            extraction = await asyncio.to_thread(extract_contract,
                path,
                document_version_id=version_id,
                engine=self.engine,
            )
            if len(extraction.pages) != source.page_count:
                raise ContractExtractionError(
                    "Extracted page count does not match the uploaded file record."
                )
            await self.repository.replace_extraction(extraction)
            return extraction
        except Exception as exc:
            message = str(exc).strip() or "Contract extraction failed."
            try:
                await self.repository.mark_extraction_failed(version_id, message)
            except Exception:  # Preserve the original extraction failure.
                logger.exception("Could not record extraction failure for %s", version_id)
            if isinstance(exc, ContractExtractionError):
                raise
            raise ContractExtractionError("Contract extraction failed.") from exc
