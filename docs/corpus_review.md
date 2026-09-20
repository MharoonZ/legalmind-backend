# Contract Act corpus review

The source PDF is [Pakistan Code's Contract Act, 1872](https://pakistancode.gov.pk/pdffiles/administrator8332a6df32386960ac7d81a5cf7aade2.pdf). The copied file is `data/sources/contract_act_1872_pakistan_code.pdf`; its SHA-256 is `930c60f67216530fab28f37d439ad3c3ae941dc918c914f2e7c18ddbabb7bfbd`. The [Pakistan Code disclaimer](https://pakistancode.gov.pk/english/Xki72HF.php) says website content is under review. Treat this extraction as a draft until a knowledgeable reviewer accepts the source and transcription.

## What the scripts do

- `python scripts/build_contract_act_corpus.py` checks the PDF hash, extracts sections 1 through 30, and writes `data/contract_act_1872_sections_1_30.json`.
- `node scripts/build_contract_act_review.mjs` creates a workbook under `outputs/haroon-sprint1-20260919/`. It contains section text, PDF page ranges, page-specific evidence passages, hashes, and review fields.
- `alembic upgrade head` creates the corpus tables and the `vector` extension. It never runs at API startup.
- `python scripts/ingest_contract_act_corpus.py --check-only` verifies the JSON and PDF without database access. Without `--check-only`, the script imports the draft into PostgreSQL in one transaction. Re-running an unchanged import is safe.

The extraction uses this PDF's 12-point body type to exclude its 8-point amendment footnotes and page labels. It retains illustrations and splits passages where a section crosses a PDF page. It keeps the thirty numbered sections 1–30 separate from inserted section 19A, and stops at section 30A. PDF pages 1–10 are contents pages; section 1 starts on PDF page 11. Section 30 continues onto page 24. Printed line-end hyphens are preserved with a newline for review.

## Human review before approval

1. Confirm the chosen PDF and its jurisdiction/version with the supervisor or qualified legal reviewer. Record their name and review date.
2. Compare **every** section row against the PDF, including its heading, clauses, illustrations, brackets, and page boundary. Section 16 starts with a bracketed amendment; section 30 continues onto page 24 before sections 30A–30C, which are outside this sprint. Section 19A is an inserted section on page 18: it is excluded from the 30 numbered rows and needs an explicit team/supervisor scope decision before approval.
3. Pay special attention to printed line-end hyphens in sections 3, 16, and 20, and to superscript amendment notes. The draft preserves the printed main text but excludes small footnotes from retrieval passages.
4. Record corrections and reviewer decisions in the workbook. A workbook edit does **not** alter database evidence. Correct the extraction or corpus input, regenerate the JSON/workbook, and re-run tests so new content gets new hashes and IDs.
5. Only after review, approve the matching corpus version in the database through a separate, recorded review process. The import script always leaves `review_status = needs_review`; retrieval must filter for approved versions.

The section text and evidence IDs are immutable for a given corpus version. A corrected transcription creates a different corpus SHA-256 and different IDs. This prevents an old citation from silently changing its meaning.
