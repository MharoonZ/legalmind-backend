# Local corpus extraction and inspection

This work extends Haroon's upload flow. It creates page/block outcomes for an
existing queued `extract` job. It does not parse provisions or modify `/ask`,
private-contract processing, chunks, embeddings, publications or human approvals.
No new migration is needed: the worker reuses `processing_jobs`, `job_attempts`,
`file_extractions`, `file_pages` and `file_blocks`.

## Setup on Windows

Run from `D:\Haroon legalMind\legalmind-backend`. Use the existing `.venv` or:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Privately place the correct backend `.env` in this directory. Do not overwrite
an existing environment file with the example. `DATABASE_URL` is required;
the existing corpus migration must already be present. Shared migrations need
the database owner's explicit approval. `.env`, `.venv` and `uploads` are ignored.
For local inspection set these non-secret values in `.env` or the server shell:

```dotenv
ENVIRONMENT=development
LEGAL_INGEST_ENABLED=true
CORS_ORIGINS=http://localhost:3000
LEGAL_UPLOAD_DIR=uploads/legal
```

If the database was registered on another machine, copy the **existing** PDF
blob into the same relative content-addressed path under `LEGAL_UPLOAD_DIR`.
The worker's `--inspect` command prints the recorded object key and whether it
exists. Do not register a replacement or alter `legal_files` to make it fit.
Size and SHA-256 are verified before opening the PDF and on each page operation.

Install Tesseract separately using the platform guidance in the
[Tesseract installation documentation](https://tesseract-ocr.github.io/tessdoc/Installation.html).
The Python dependencies do not install its executable or language files.
Configure paths privately in the backend `.env` (forward slashes work on Windows):

```dotenv
LEGAL_TESSERACT_PATH=C:/Program Files/Tesseract-OCR/tesseract.exe
LEGAL_TESSDATA_DIR=C:/Program Files/Tesseract-OCR/tessdata
LEGAL_OCR_LANGUAGES=eng
LEGAL_OCR_ENABLED=true
LEGAL_OCR_DPI=200
```

Use `eng+urd` only if both trained language files are installed. The worker
checks `--version` and `--list-langs` before claiming work; language availability
is part of the hashed configuration. The
[official command-line guide](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
documents `-l`, `--tessdata-dir` and TSV output. Check this installation:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --ocr-check
```

Missing Tesseract/languages never becomes a successful OCR result. A page with
no text gets a failed outcome and explicit warning; retained native text with
an unmet OCR need gets `needs_review`. Fix prerequisites, then explicitly retry
to produce a new extraction while keeping the old outcomes.

## Start the applications

Backend shell:

```powershell
Set-Location 'D:\Haroon legalMind\legalmind-backend'
$env:LEGAL_INGEST_ENABLED='true'
$env:EMBEDDING_WARMUP='false'
$env:EMBEDDINGS_ENABLED='false'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

These shell settings avoid loading unrelated AI models for corpus inspection.
They do not rewrite `.env` or change stored search data. Check an occupied port's
owning process before stopping anything.

Frontend in another shell:

```powershell
Set-Location 'D:\Haroon legalMind\legalmind-frontend'
npm.cmd run dev -- --hostname localhost
```

Open http://localhost:3000/admin/ingest/inspect. The frontend uses the public
backend address `http://127.0.0.1:8000` by default. Never copy backend secrets
into the frontend. Neither page is an authentication system; both applications
must remain bound to loopback with the local access guard enabled.

## Inspect and run only the pilot

In a third backend shell, enable local ingestion if it is not already in `.env`:

```powershell
$env:LEGAL_INGEST_ENABLED='true'
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --job-id 6e9d562d-17bc-4fcd-b245-a0918104bfd7 --inspect
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --job-id 6e9d562d-17bc-4fcd-b245-a0918104bfd7
```

Inspect first. The first command performs reads only. The second processes a
queued job, or resumes a running job after its lease expires. An active lease
prevents a second worker from taking it. It makes no change to an already
completed/failed job. A failed attempt is queued again until its bounded attempt
budget is exhausted; repeat the same command, or add `--watch` to poll every
five seconds. `--watch --job-id ...` still selects only that job.

An explicit retry of a failed or completed job creates a new extraction run:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_legal_extraction --job-id 6e9d562d-17bc-4fcd-b245-a0918104bfd7 --retry
```

It preserves all previous pages, blocks and attempts. It refuses a running or
queued job. Do not use `--all-queued` for the pilot: that flag deliberately opts
into processing unrelated jobs. Upload/retry HTTP requests never execute extraction.

## Durability and completion

- Claims use a PostgreSQL row lock with `FOR UPDATE SKIP LOCKED`, then store a
  `JobAttempt` and a lease in the same transaction. Each write locks the job and
  checks the attempt ID, generation and unexpired lease. A stale worker cannot
  save pages or mark a reclaimed job complete.
- Heartbeats renew every third of `LEGAL_WORKER_LEASE_SECONDS` (default 120).
  Each PyMuPDF page operation runs in a bounded child process; the event loop
  remains available for heartbeats. Default page timeout is 90 seconds.
- At most `LEGAL_WORKER_MAX_ATTEMPTS` (default 3) attempts run per generation.
  Expired attempts are recorded as failed. An explicit retry resets the budget
  by starting a new generation, while lifetime attempt numbers keep increasing.
- Extraction IDs derive from job ID, generation start time and config hash.
  Automatic recovery with the same configuration resumes existing page rows.
  A config change produces a different extraction and marks the old unfinished
  run failed. Completed runs are never rewritten.
- Page and block IDs/uniqueness are deterministic within an extraction; a page
  and all its blocks commit together. Stored page outcomes are not overwritten
  during recovery. A retry is how failed/review-required pages are reprocessed.
- Output completion requires exactly every **physical** page, ordered `1..N`,
  plus a canonical output SHA-256 stored in the same completion transaction.
  Native/OCR failures still count as recorded outcomes; this is explicitly
  separate from successful transcription and human approval.
- A registered/physical page-count mismatch is reported on every processed
  page. All actual pages are processed; registry metadata is never silently
  changed. The inspector lists missing/extra indices and refuses to show
  coverage as consistent until the registered and physical counts agree.
- If the document itself cannot be opened, registered pages receive explicit
  failed outcomes, but the extraction/job cannot complete: physical coverage
  could not be checked. Missing or modified source bytes fail the attempt.

The config hash includes extractor/normalizer versions, pinned PyMuPDF version,
OCR executable/tessdata paths, detected engine/languages, language selection,
DPI, sparse-text threshold and timeout. Existing extraction columns retain
engine/version and OCR engine/languages, plus the hash. Preserve the run's local
configuration if you need to reproduce it; the existing schema has no complete
configuration JSON column. No additional column was added by this task.

## Text, geometry and review limits

Native extraction includes all physical pages and text sizes. Image blocks are
retained as `figure`; text/OCR blocks use conservative kind `other`. This is not
a section parser. Coordinates are points on the rotated original page; order is
geometric (top-to-bottom, then left-to-right), not a proof of legal reading order.
Columns, tables, marginal notes and unusual PDF encodings require visual review.

Normalization changes CRLF/CR to LF and removes trailing spaces/tabs per line.
It does not correct spelling, merge hyphenated words, change punctuation, or
rewrite legal wording. Blocks are joined with two LF separators. Character
offsets refer exactly to that stored normalized page string; each nonempty
block's offsets and SHA-256 are validated before saving. Raw extracted text is
also retained. Hashes do not establish correctness.

Native text below 40 alphanumeric characters, replacement/control corruption,
or substantial image content (at least 20% of page area alongside native text)
triggers OCR. These are conservative heuristics, **not validated accuracy
thresholds**. Mixed pages keep native text and add OCR words outside native
bounding boxes; the overlap heuristic may need manual review. OCR-only pages
use Tesseract line output. OCR results always require review. No language model
is called. `ocr_confidence` stays NULL: per-word TSV confidence is not presented
as an invented page-level score. A failure before page geometry can be read
leaves dimensions/rotation NULL with a failure warning. The worker never writes
`verified_blank`, excluded pages, review events or approval fields.

## Read-only APIs

All routes have prefix `/local-admin/legal`, use the same development/loopback
and allowed-Origin restrictions as upload, and remain hidden from public OpenAPI:

| GET route | Result |
| --- | --- |
| `/documents?limit=25&offset=0` | Paginated registered files and recent run/job metadata |
| `/documents/{file_id}` | One file's registry metadata, jobs and extractions |
| `/jobs/{job_id}` | Progress, lease, errors, timestamps and attempts |
| `/extractions/{extraction_id}` | Page summaries, coverage and outcome counts |
| `/extractions/{extraction_id}/pages/{page_index}` | Raw/normalized text, warnings and ordered blocks |
| `/documents/{file_id}/pages/{page_index}/preview` | PNG rendered from verified original bytes; no-store |

IDs are validated UUIDs. Preview paths come only from the DB record and configured
root; traversal, unexpected object keys and symlink escapes are rejected. There
is no arbitrary-file path parameter, PDF replacement endpoint or HTTP mutation.

## Tests and real-pilot distinction

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

The new tests create synthetic native, scanned, mixed, empty and corrupt PDFs.
OCR-success tests deliberately mock Tesseract; real native extraction and PNG
preview rendering use PyMuPDF 1.26.3. Repository tests persist to disposable
SQLite files with a transaction adapter, exercising retries, progress, fencing,
recovery and idempotency. SQLite does **not** prove PostgreSQL locking or its
full constraint behavior. A separate optional concurrency test uses a random
schema in an explicitly configured, disposable **local** PostgreSQL database:

```powershell
# Configure LEGAL_EXTRACTION_TEST_DATABASE_URL privately in this shell.
# It must use loopback and a database name starting with legalmind_test.
.\.venv\Scripts\python.exe -m pytest -q tests/test_legal_extraction_postgres.py
```

That optional test never falls back to `DATABASE_URL`/`.env`; without the separate
test URL it is explicitly skipped. Do not point it at the shared database.
Browser fixture tests and native extraction tests are not legal accuracy tests.
The real pilot requires its exact original PDF, reachable database and Tesseract.
Compare all six original pages visually with the returned text, especially the
page lacking selectable text. OCR output is not approval.

After page-level review, the subsequent legal-structure stage can create
provisions/hierarchy and source spans, then review/approve a corpus revision.
Those stages are intentionally not implemented here.
