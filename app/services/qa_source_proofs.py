"""Shared character-proof checks for source reads and final citation assembly."""
from dataclasses import dataclass
from uuid import UUID


class CitationVerificationError(RuntimeError):
    """A draft claim is unsupported or its source mapping cannot be proved."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceProof:
    start: int
    end: int
    provision_id: UUID
    kind: str
    label: str | None
    reference_path: str | None
    source_span_id: UUID
    file_id: UUID
    file_sha256: str
    filename: str
    page_index: int
    source_url: str | None


def valid_text_range(text: str, start: int, end: int) -> bool:
    """Python slices silently clip; exact source mappings must not do so."""
    return (isinstance(start, int) and not isinstance(start, bool) and
            isinstance(end, int) and not isinstance(end, bool) and 0 <= start < end <= len(text))


def covered_proofs(text: str, start: int, end: int, proofs: list[SourceProof]) -> tuple[SourceProof, ...]:
    """Every non-whitespace quoted character must map to an exact source block."""
    if not valid_text_range(text, start, end):
        raise CitationVerificationError("quotation_range_invalid")
    if any(not valid_text_range(text, row.start, row.end) for row in proofs):
        raise CitationVerificationError("source_mapping_not_exact")
    selected = sorted((row for row in proofs if row.end > start and row.start < end),
                      key=lambda row: (row.start, row.end))
    cursor = start
    for row in selected:
        if row.start > cursor and text[cursor:min(row.start, end)].strip():
            raise CitationVerificationError("source_span_gap")
        cursor = max(cursor, min(row.end, end))
    if not selected or text[cursor:end].strip():
        raise CitationVerificationError("source_span_gap")
    return tuple(selected)
