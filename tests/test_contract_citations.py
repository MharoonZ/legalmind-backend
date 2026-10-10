"""Tests for strict uploaded-contract citation validation."""

import pytest

from app.core.errors import ContractCitationError
from app.services.contract_citations import (
    ContractClauseEvidenceSource,
    validate_contract_citation,
    validate_contract_citations,
)
from app.services.contract_extraction import ClausePageSpan

DOCUMENT_ID = "doc:one"
VERSION_ID = "docver:one"
CLAUSE_ID = "cl:services"
CLAUSE_TEXT = (
    "1. Services\nProvider will deliver design work.\n\n"
    "The work continues after client review."
)
PAGE_TWO_START = CLAUSE_TEXT.index("The work")
CLAUSE = ContractClauseEvidenceSource(
    document_id=DOCUMENT_ID,
    version_id=VERSION_ID,
    clause_id=CLAUSE_ID,
    text=CLAUSE_TEXT,
    spans=(
        ClausePageSpan(1, 0, PAGE_TWO_START),
        ClausePageSpan(2, PAGE_TWO_START, len(CLAUSE_TEXT)),
    ),
)


def _claim(**updates):
    claim = {
        "document_id": DOCUMENT_ID,
        "version_id": VERSION_ID,
        "clause_id": CLAUSE_ID,
        "quote": "design work. The work continues",
        "pages": [1, 2],
    }
    claim.update(updates)
    return claim


def test_exact_quote_spanning_pages_gets_server_evidence_id():
    citation = validate_contract_citation(
        _claim(),
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        clauses={CLAUSE_ID: CLAUSE},
    )
    assert citation.evidence_id.startswith("cev:")
    assert citation.page_numbers == (1, 2)
    assert citation.quote == "design work.\n\nThe work continues"
    assert CLAUSE_TEXT[citation.clause_char_start : citation.clause_char_end] == citation.quote


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"document_id": "doc:other"}, "different document"),
        ({"version_id": "docver:other"}, "different document version"),
        ({"clause_id": "cl:missing"}, "does not exist"),
        ({"quote": "invented wording"}, "not present"),
        ({"quote": "Design work. The work continues"}, "not present"),
        ({"pages": [2]}, "page references"),
        ({"evidence_id": "cev:made-up"}, "evidence ID"),
    ],
)
def test_mismatched_citation_is_rejected(updates, message):
    with pytest.raises(ContractCitationError, match=message):
        validate_contract_citation(
            _claim(**updates),
            document_id=DOCUMENT_ID,
            version_id=VERSION_ID,
            clauses={CLAUSE_ID: CLAUSE},
        )


def test_bulk_validation_drops_bad_and_duplicate_citations():
    valid = _claim()
    result = validate_contract_citations(
        [valid, _claim(quote="not in clause"), valid],
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        clauses={CLAUSE_ID: CLAUSE},
    )
    assert len(result) == 1
    assert result[0].clause_id == CLAUSE_ID
