# Sprint 2 - Contract extraction and citation truth (Haroon)

## Scope delivered

- Docling adapter for selectable-text PDFs, with OCR explicitly disabled.
- Canonical full text and SHA-256 for every PDF page.
- Deterministic, version-scoped clause IDs that preserve original labels.
- Structured headings and Markdown tables in clause text.
- Character ranges mapping every clause block to its source PDF page.
- Strict citation validation across document, version, clause, quotation, and pages.
- Server-generated evidence IDs after validation; model-supplied IDs are never trusted.

Summary generation is not part of this module. Abdullah's summary step can load
the stored clauses and call `validate_contract_citations` before saving or
returning evidence.

## Database setup

```powershell
alembic upgrade head
```

Migration `20260927_04` adds:

- extraction metadata on `document_versions`
- `contract_pages`
- `contract_clauses`
- `contract_clause_spans`

The new tables only reference `document_versions`; they never join uploaded
contract text into the `law_*` corpus used by `/ask`.

## Extract an uploaded version

```powershell
python scripts/extract_contract.py --version-id "docver:<id>"
```

The status transition is:

```text
uploaded -> extracting -> extracted
                       -> extraction_failed
```

The saved PDF hash and page count are rechecked before results are committed.
Replacing extraction output is transactional, so partial pages or clauses are
not left behind. Once a version reaches `extracted`, its clause IDs are
immutable; corrected or reprocessed content must use a new document version.

## Preview one sample PDF

```powershell
python scripts/extract_contract.py --pdf path\to\sample-service-agreement.pdf
```

Preview mode prints clause IDs, labels, text, and page mappings as JSON and
does not write to PostgreSQL.

## Citation contract for the summary step

Each claimed citation must provide:

```json
{
  "document_id": "doc:...",
  "version_id": "docver:...",
  "clause_id": "cl:...",
  "quote": "exact contract wording",
  "pages": [1, 2]
}
```

`validate_contract_citation` rejects unknown or cross-document clauses,
invented quotations, wrong page references, and forged evidence IDs. On
success it returns the exact stored text span, verified pages, offsets, and a
deterministic `cev:...` evidence ID.

## Tests

```powershell
pytest -q tests/test_contract_extraction.py tests/test_contract_citations.py
pytest -q
```

The tests use an injected fake structured extractor. This keeps unit tests
fast and deterministic while the production adapter remains Docling.
