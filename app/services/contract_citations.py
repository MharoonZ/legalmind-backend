"""Strict citation validation for uploaded contracts.

Unlike the Sprint 1 law-corpus validator, this validator does not repair a
model's quotation. A contract summary may cite a span only when its document,
version, clause, exact text, and optional claimed pages all agree. Evidence IDs
are generated here after validation; model-supplied identifiers are not trusted.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from app.core.errors import ContractCitationError
from app.services.contract_extraction import ClausePageSpan

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContractClauseEvidenceSource:
    document_id: str
    version_id: str
    clause_id: str
    text: str
    spans: tuple[ClausePageSpan, ...]


@dataclass(frozen=True, slots=True)
class ValidatedContractCitation:
    evidence_id: str
    document_id: str
    version_id: str
    clause_id: str
    quote: str
    clause_char_start: int
    clause_char_end: int
    page_numbers: tuple[int, ...]


def _normalised_with_source_map(text: str) -> tuple[str, list[int], list[int]]:
    """Collapse whitespace while retaining a map to exact source characters."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    whitespace_start: int | None = None

    for index, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] != " " and whitespace_start is None:
                whitespace_start = index
            continue

        if whitespace_start is not None:
            chars.append(" ")
            starts.append(whitespace_start)
            ends.append(index)
            whitespace_start = None
        chars.append(char)
        starts.append(index)
        ends.append(index + 1)

    return "".join(chars), starts, ends


def _normalise_quote(quote: str) -> str:
    return " ".join(quote.split()).strip()


def _claim_pages(item: dict) -> tuple[int, ...] | None:
    raw = item.get("page_numbers", item.get("pages"))
    if raw is None:
        page = item.get("page")
        raw = None if page is None else [page]
    if raw is None:
        return None
    if isinstance(raw, (str, int)):
        raw = [raw]
    try:
        pages = tuple(sorted({int(value) for value in raw}))
    except (TypeError, ValueError) as exc:
        raise ContractCitationError("Citation pages must be positive integers.") from exc
    if not pages or any(page < 1 for page in pages):
        raise ContractCitationError("Citation pages must be positive integers.")
    return pages


def _pages_for_range(
    spans: tuple[ClausePageSpan, ...], start: int, end: int
) -> tuple[int, ...]:
    pages = {
        span.page_number
        for span in spans
        if span.clause_char_start < end and span.clause_char_end > start
    }
    if not pages:
        raise ContractCitationError("Citation text has no stored PDF page provenance.")
    return tuple(sorted(pages))


def _evidence_id(
    *, version_id: str, clause_id: str, start: int, end: int, quote: str
) -> str:
    raw = f"{version_id}\0{clause_id}\0{start}\0{end}\0{quote}".encode("utf-8")
    return "cev:" + hashlib.sha256(raw).hexdigest()[:32]


def validate_contract_citation(
    item: dict,
    *,
    document_id: str,
    version_id: str,
    clauses: dict[str, ContractClauseEvidenceSource],
) -> ValidatedContractCitation:
    """Validate one claimed quote and return server-generated evidence."""
    if not isinstance(item, dict):
        raise ContractCitationError("Citation must be an object.")
    if str(item.get("document_id", "")).strip() != document_id:
        raise ContractCitationError("Citation belongs to a different document.")
    if str(item.get("version_id", "")).strip() != version_id:
        raise ContractCitationError("Citation belongs to a different document version.")

    clause_id = str(item.get("clause_id", "")).strip()
    clause = clauses.get(clause_id)
    if clause is None:
        raise ContractCitationError("Citation clause does not exist in this version.")
    if clause.document_id != document_id or clause.version_id != version_id:
        raise ContractCitationError("Citation clause belongs to another document version.")

    claimed_quote = _normalise_quote(str(item.get("quote", "")))
    if not claimed_quote:
        raise ContractCitationError("Citation quote is empty.")

    flat_text, starts, ends = _normalised_with_source_map(clause.text)
    claimed_pages = _claim_pages(item)

    search_from = 0
    selected: tuple[int, int, tuple[int, ...]] | None = None
    while True:
        flat_start = flat_text.find(claimed_quote, search_from)
        if flat_start < 0:
            break
        flat_end = flat_start + len(claimed_quote)
        source_start = starts[flat_start]
        source_end = ends[flat_end - 1]
        pages = _pages_for_range(clause.spans, source_start, source_end)
        if claimed_pages is None or pages == claimed_pages:
            selected = (source_start, source_end, pages)
            break
        search_from = flat_start + 1

    if selected is None:
        if claimed_pages is not None and claimed_quote in flat_text:
            raise ContractCitationError("Citation page references do not match the quote.")
        raise ContractCitationError("Citation quote is not present in the claimed clause.")

    source_start, source_end, pages = selected
    exact_quote = clause.text[source_start:source_end]
    evidence_id = _evidence_id(
        version_id=version_id,
        clause_id=clause_id,
        start=source_start,
        end=source_end,
        quote=exact_quote,
    )
    supplied_id = str(item.get("evidence_id", "")).strip()
    if supplied_id and supplied_id != evidence_id:
        raise ContractCitationError("Citation evidence ID does not match the quote span.")

    return ValidatedContractCitation(
        evidence_id=evidence_id,
        document_id=document_id,
        version_id=version_id,
        clause_id=clause_id,
        quote=exact_quote,
        clause_char_start=source_start,
        clause_char_end=source_end,
        page_numbers=pages,
    )


def validate_contract_citations(
    raw_citations: list[dict],
    *,
    document_id: str,
    version_id: str,
    clauses: dict[str, ContractClauseEvidenceSource],
    max_citations: int = 10,
) -> list[ValidatedContractCitation]:
    """Keep only fully valid, unique citations for the requested version."""
    validated: list[ValidatedContractCitation] = []
    seen: set[str] = set()
    for item in raw_citations:
        try:
            citation = validate_contract_citation(
                item,
                document_id=document_id,
                version_id=version_id,
                clauses=clauses,
            )
        except ContractCitationError as exc:
            logger.warning("Dropped invalid contract citation: %s", exc)
            continue
        if citation.evidence_id in seen:
            continue
        seen.add(citation.evidence_id)
        validated.append(citation)
        if len(validated) >= max_citations:
            break
    return validated
