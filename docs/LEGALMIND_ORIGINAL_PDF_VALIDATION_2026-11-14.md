# Original PDF restoration and validation

Completed on 14 November 2026 using the user's folder
`C:\Users\Admin\Downloads\uploads`.

## Restoration

The folder contained 46 physical PDF copies with 23 unique content hashes.
All 23 matched the registered source hashes, byte sizes and page counts.
They were copied atomically into `legalmind-backend/uploads/legal` under their
existing storage keys. All 46 Downloads copies remained hash-identical. A
repeat restoration created zero files. No database records, IDs, approvals,
chunks, embeddings or original source copies were changed.

## Complete active collection inspected

Byte and fresh extraction checks covered every active source below. The active
publication contains 199 physical pages; the larger historical page-row count
includes earlier extraction attempts.

| Original filename | Published instrument | Physical pages |
| --- | --- | ---: |
| 112.pdf | The Punjab Control of Goondas Ordinance, 1959 | 12 |
| 127.pdf | The Punjab Maintenance of Public Order Ordinance, 1960 | 16 |
| 13.pdf | The Prisons Act, 1894 | 19 |
| 130.pdf | The Punjab Suppression of Prostitution Ordinance, 1961 | 7 |
| 131.pdf | The Punjab Anti-Corruption Establishment Ordinance, 1961 | 5 |
| 14.pdf | The Reformatory Schools Act, 1897 | 10 |
| 17.pdf | The Prisoners Act, 1900 | 15 |
| 190.pdf | The Punjab Arms Ordinance, 1965 | 14 |
| 221.pdf | The Criminal Law (Special Provisions) Ordinance, 1968 | 11 |
| 26.pdf | The Restriction of Habitual Offenders (Punjab) Act, 1918 | 6 |
| 2712.pdf | The Punjab Criminal Prosecution Service Inspectorate Act 2018 | 5 |
| 275.pdf | The Punjab Control of Goondas (Removal of Doubts) Ordinance, 1972 | 1 |
| 329.pdf | The Punjab Prevention of Gambling Ordinance, 1978 | 5 |
| 362.pdf | The Public Gambling (Repeal) Act, 1985 | 3 |
| 37.pdf | The Good Conduct Prisoners' Probational Release Act, 1926 | 5 |
| 38.pdf | The Punjab Borstal Act, 1926 | 13 |
| 393.pdf | The Punjab Prohibition of Expressing Matters on Walls Act, 1995 | 3 |
| 453.pdf | The Punjab Private Security Companies (Regulation and Control) Ordinance, 2002 | 9 |
| 477.pdf | The Punjab Consumer Protection Act 2005 | 13 |
| 483.pdf | The Punjab Criminal Prosecution Service (Constitution, Functions and Powers) Act 2006 | 9 |
| 94.pdf | The Punjab Vagrancy Ordinance, 1958 | 9 |
| 97.pdf | The Prevention of Corruption Act (West Pakistan Extension) Ordinance, 1958 | 3 |
| punjab-murderous-outrages-act-1867.pdf | The Punjab Murderous Outrages Act, 1867 | 6 |

## Results

- Original bytes and registered metadata: all 23 matched.
- Fresh extraction: all 199 raw pages and all 1,548 raw blocks matched stored text.
- Fresh methods: 182 pages with native text and 17 pages with no text, flagged as blank candidates.
- Normalized text: 198 of 199 pages matched exactly. The remaining page has one historical reviewed glyph substitution, described below.
- Source lineage: 2,473 active chunk/provision/span links checked, zero mismatches.
- Embeddings: all 531 checked against their chunk/revision IDs, pinned BGE-M3 profile, 1,024-dimensional width, normalized vector magnitude and assembled-input hash.
- Maximum complete embedding input: 1,016 tokens, within the pinned 8,192-token limit.
- Chunk-root sequences: zero failures. No embedding regeneration or reprocessing was necessary.
- Prior real API citations: all 19 distinct quoted fragments matched their original physical PDF pages, with no unmatched file/page inputs.
- Real original-preview API: Consumer Protection physical page 5 returned HTTP 200, PNG content and no-store caching.

The full original comparison took 196.049 seconds, including DB reads, fresh
extraction and tokenizer loading. This is an offline audit duration, not an
answer-response measurement. No new RAG performance claim is made here.

## Glyph discrepancy and quotation fix

On physical page 3 of `393.pdf`, the preserved PDF text layer reads `(l)` in
the first subsection marker. Stored normalized text uses `(1)`, with an existing
`visually_corrected_glyph` review record dated 8 October. Raw text is preserved.

The approved normalization and its identifiers were retained. Citation proof now
also checks the actual quoted range against preserved raw source characters.
A reviewed replacement cannot silently become a literal quotation. Unchanged
words in the same source block remain citable. Canonical Unicode and line-ending
normalization remain supported. Unsupported offset-changing corrections fail
explicitly instead of being guessed.

A real DB check confirmed that a quotation containing this substituted marker
is rejected and that the unchanged following words still verify. The two new
regressions cover both behaviors. Three restoration tests cover idempotency,
refusing conflicting existing files and preventing storage-key path traversal.
Focused restoration/inspection checks passed 10 tests; focused citation/source
checks passed 68 tests. The full backend suite was not repeated for these changes.

Six original pages received visual spot checks: Consumer Protection pages 5, 6
and 7; Prisons Act pages 10 and 11; and the glyph discrepancy on `393.pdf` page 3.
These supplement the collection-wide automated comparison. They do not establish
that every legal interpretation, table structure or edition is legally current.
All 23 editions still have unknown applicability metadata.

The runtime verifies citations against publication-pinned stored source data,
including preserved raw text. This audit separately establishes that those raw
pages match the restored original PDFs; it does not reread every original PDF
on every answer request.

## Reproduction

Run from the backend directory:

~~~powershell
.\.venv\Scripts\python -m scripts.audit_uploaded_pdfs --source-dir 'C:\Users\Admin\Downloads\uploads' --restore-missing --output restore-check.json
.\.venv\Scripts\python -m scripts.verify_original_pdf_corpus --output originals-check.json --responses ..\tmp\implementation-live-final.json
~~~

The inventory defaults to read-only. Only the explicit --restore-missing option
copies source bytes, and it never replaces an existing blob or changes the DB.
The original-corpus verifier performs no writes to source files or the database.

Machine-readable results: [original validation](verification/implementation_20261114_original_validation.json),
[restoration](verification/implementation_20261114_pdf_restore.json),
[glyph review](verification/implementation_20261114_glyph_review.json),
[literal quotation check](verification/implementation_20261114_original_guard.json),
and [actual preview response](verification/implementation_20261114_original_preview.json).
