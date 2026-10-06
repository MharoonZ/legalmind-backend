# Corpus extraction and inspector handoff - 6 October 2026

## Source and implementation branches

Only the new copies under `D:\Haroon legalMind` were used for this task.

| Repository | Source branch | Source commit | Implementation branch |
| --- | --- | --- | --- |
| Backend | `origin/haroon` | `38885e54cfcb6f550da115e5df9c2237a147cca4` | `shimza-corpus-extraction` |
| Frontend | `origin/haroon` | `b4010a30156a9d02fdeaf375827a8d8483b15115` | `shimza-corpus-extraction` |

These were the source commits for implementation and local verification.
The shared delivery branch is `shimza` in each repository;
`shimza-corpus-extraction` retains the local implementation checkpoint.
Publication to `shimza` was subsequently authorized by the user. No PR or merge
to `main` is part of this delivery. Earlier project copies were not edited.
The virtual environment and dependency installations for these new copies are ready.

## What changed

Backend modified files:

- `.env.example`: non-secret worker/OCR configuration examples.
- `README.md`: route corpus work to its own setup guide.
- `app/core/config.py`: bounded lease, retry, timeout, OCR and preview settings.
- `app/main.py`: register the local-only inspector router.
- `docs/HAROON_LEGAL_CORPUS_SCHEMA.md`: correct the outdated worker/inspector status.

Backend added files:

- `app/services/legal_pdf_extraction.py`: original-file integrity, all-page native
  extraction, conservative normalization, mixed/scanned-page OCR and original rendering.
- `app/services/pdf_process.py`: bounded child processes; works with Windows
  Uvicorn reload mode without putting PyMuPDF objects on shared threads.
- `app/services/legal_extraction_worker.py`: per-page processing with lease renewal.
- `app/repositories/legal_extraction_repository.py`: job claims, fenced writes,
  attempts, progress, resume/retry, exact coverage and output hashes.
- `app/repositories/legal_inspection_repository.py`: read-only document/run/page queries.
- `app/api/routes/legal_inspection.py`: protected read/preview endpoints.
- `scripts/run_legal_extraction.py`: explicit job selection, inspection, processing,
  bounded automatic recovery, explicit retry and OCR prerequisite check.
- `tests/test_legal_pdf_extraction.py`: native/scanned/mixed/empty/corrupt fixtures,
  block offsets, integrity, OCR routing and Windows reload preview regression.
- `tests/test_legal_extraction_repository.py`: real local ORM persistence for
  idempotency, retries, expired-worker fencing, attempts and progress.
- `tests/test_legal_extraction_worker.py`: six-page coverage, cancellation recovery,
  heartbeat, corruption, count discrepancies and integrity failures.
- `tests/test_legal_inspection_api.py`: access restrictions, validated IDs and original previews.
- `tests/test_legal_extraction_postgres.py`: optional isolated PostgreSQL concurrency test.
- `docs/LEGAL_PDF_EXTRACTION.md`: setup, semantics, API list and run/retry instructions.
- `docs/CORPUS_EXTRACTION_HANDOFF.md`: this delivery record.

Frontend modified files:

- `README.md`: local inspector setup and scope.
- `src/app/admin/ingest/page.tsx`: reuse the existing access check through a shared helper.
- `src/app/admin/ingest/legal-ingest-form.tsx`: inspector links and accurate queue guidance.

Frontend added files:

- `src/lib/local-admin.ts`: shared development/loopback page guard.
- `src/lib/legal-inspection.ts`: backend response types and read requests.
- `src/app/admin/ingest/inspect/page.tsx`: guarded inspector route.
- `src/app/admin/ingest/inspect/pdf-inspector.tsx`: original/text comparison,
  navigation, counts, warnings, raw text and block details.
- `tests/legal-inspector.spec.ts`: desktop/mobile fixture tests for completion,
  incomplete/failed pages, missing originals, errors, loading and stale responses.

The existing design, question page, `/ask` implementation, models and migrations
were left intact. No provisions, chunks, embeddings, indexes or login were added.

## APIs added

All are GET-only under `/local-admin/legal` and reuse upload's development,
loopback client, Host and configured Origin restrictions:

1. `/documents?limit=25&offset=0`
2. `/documents/{file_id}`
3. `/jobs/{job_id}`
4. `/extractions/{extraction_id}`
5. `/extractions/{extraction_id}/pages/{page_index}`
6. `/documents/{file_id}/pages/{page_index}/preview`

The documented CLI handles retries; there is no synchronous extraction HTTP
endpoint. Preview requests return PNGs from the hash-verified original bytes.

## Verification

- Backend: **170 passed, 1 skipped**. The skipped test needs a separate,
  disposable local PostgreSQL database for concurrent-worker row-lock testing.
  SQLite persistence tests do not substitute for that guarantee.
- Dependency compatibility: `pip check` passed.
- Frontend: typecheck, lint and production build passed.
- Browser regression suite: **52 passed, 4 skipped** on desktop/mobile Chromium.
  The four skipped tests call the unrelated live AI backend. New inspector
  success/failure UI tests use explicit fixtures, not invented pilot results.
- Live API: document/job reads and all six original-page previews return 200;
  `http://localhost:3000` receives the matching CORS header and an unapproved
  Origin receives 403. Missing original bytes produce a helpful 404.
- The live inspector passed checks of all six actual pilot pages on both
  desktop and mobile (12 page checks): original images loaded, displayed text
  exactly matched each API response, page 2's warning remained visible, and
  neither viewport had JavaScript errors or horizontal overflow.
- Real native PDF rendering/extraction uses pinned PyMuPDF 1.26.3. Successful
  OCR tests use a mocked Tesseract response. Tesseract is **not installed locally**,
  so real OCR execution remains unverified here.
- Existing dependency deprecation warnings remain. `npm ci` reported 6 audit
  findings in the inherited dependency tree (5 high, 1 critical); no unrelated
  dependency upgrade was made by this task.

## Real pilot: existing records inspected, not reprocessed

Document: The Punjab Murderous Outrages Act, 1867.

- File ID: `834e10c8-990d-4a1d-82b1-abec590a1a45`.
- Job ID: `6e9d562d-17bc-4fcd-b245-a0918104bfd7`.
- Existing extraction ID: `977594b3-e538-4e84-a030-5ea7b0ae9fdf`.
- Original size: **266,297 bytes**, six physical pages.
- Original SHA-256: `37fa6ca65a2520ea25e74fa9688f47bbf8cca36ab44d00edb7c929c2c033049c`.
- The original was initially missing. It was recovered using the source URL
  already stored in `legal_files`, on the official Punjab Laws site. Size,
  SHA-256 and page count matched before saving. No replacement registration
  or new file identity was created.
- Saved beneath `uploads/legal/37/37fa6ca65a2520ea25e74fa9688f47bbf8cca36ab44d00edb7c929c2c033049c.pdf`.

The initial read showed queued/0 of 6/no attempts. A later read showed completed
with one completed attempt. **This task did not invoke a worker against that
shared job**; the completion appeared while other local work was in progress.
Its existing results were preserved rather than retried or overwritten.

| Physical page | Stored method | Stored outcome | Stored text characters | Original/text observation |
| --- | --- | --- | --- | --- |
| 1 | native | extracted | 1,017 | Contents page; headings and entries visible in the original and stored text |
| 2 | none | needs_review | 0 | Original renders white; no native text, images or vector drawings detected; `blank_candidate` warning remains |
| 3 | native | extracted | 4,288 | Main text and small footnotes are present |
| 4 | native | extracted | 3,309 | Main text, running header and footnotes are present |
| 5 | native | extracted | 3,639 | Main text, running header and footnotes are present |
| 6 | native | extracted | 1,371 | Final text and footnotes are present |

All six original pages were rendered and visually inspected. For each page,
the stored string matched native extraction after ignoring whitespace; stored
text SHA-256 and block-to-page offsets also matched. All six original-preview
API responses returned valid PNGs. These are transcription consistency checks,
not legal accuracy testing or proof of a legally authoritative edition.

**Page 2 was not marked `verified_blank`.** Its appearance is an observation,
not a recorded human decision. A reviewer must explicitly decide its outcome.
No OCR-generated pilot text is present in these stored page methods.

## Database operations and remaining work

This task executed shared-database **reads only** (health, registry, job,
extraction, page and block queries). It did not claim/retry a live job, insert
page records, apply a migration, approve text, delete data or modify the active
search corpus. Test writes were confined to disposable local SQLite fixtures.
No migration was prepared: existing tables were sufficient.

Remaining prerequisites are a local Tesseract executable/languages for real OCR
testing, a disposable local PostgreSQL instance for the explicit concurrency
test, and human review of all pages, especially the textless page 2. The default
OCR thresholds and geometric reading order have not been calibrated for a
large legal corpus. Future parsing must account for headers, footnotes,
superscripts, columns and continuation pages before approval.

Next legal-structure stage: build provisions and hierarchy, map source spans
back to pages/blocks, resolve references, then implement explicit reviewer
decisions and revision approval. That stage has not been implemented here.

## Local URLs and restart commands

- Inspector: http://localhost:3000/admin/ingest/inspect
- Registration: http://localhost:3000/admin/ingest
- API: http://127.0.0.1:8000

Backend:

```powershell
Set-Location 'D:\Haroon legalMind\legalmind-backend'
$env:LEGAL_INGEST_ENABLED='true'
$env:EMBEDDING_WARMUP='false'
$env:EMBEDDINGS_ENABLED='false'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Frontend, in another terminal:

```powershell
Set-Location 'D:\Haroon legalMind\legalmind-frontend'
npm.cmd run dev -- --hostname localhost
```

Worker, from the backend directory:

```powershell
$env:LEGAL_INGEST_ENABLED='true'
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --ocr-check
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --job-id 6e9d562d-17bc-4fcd-b245-a0918104bfd7 --inspect
# For a queued/expired job, process or resume:
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --job-id 6e9d562d-17bc-4fcd-b245-a0918104bfd7
# The pilot is already completed. Only deliberately create another run with:
# .\.venv\Scripts\python.exe -m scripts.run_legal_extraction --job-id 6e9d562d-17bc-4fcd-b245-a0918104bfd7 --retry
```

Check an occupied port's owning process before stopping it. Secrets remain in
the ignored backend `.env`; the frontend only uses the public API base URL.
The changed-file secret scan found no copied `.env` credential values.
