# Local Act/Rules PDF upload and page extraction (Haroon, steps 1-2)

The backend now supports registering many Acts/Rules PDFs, one upload at a
time. It validates the original PDF, stores it under a SHA-256-derived path,
records jurisdiction/category/instrument/edition/file assignment, and creates
one queued extraction job per unique PDF. A separate worker now extracts every
physical page. The uploaded Act is **not** searchable in `/ask` yet.

## Run locally

1. Confirm the database is at migration `20261005_05` or later.
2. Set `LEGAL_INGEST_ENABLED=true` in the backend's local `.env`. Do not use
   this on a public server. Bind the backend to `127.0.0.1`, not `0.0.0.0`:

   `uvicorn app.main:app --host 127.0.0.1 --port 8000`

3. Install Tesseract language data for OCR. Put `eng.traineddata` from the
   official [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)
   repository in `uploads/ocr/tessdata/` under the backend directory, or set
   `LEGAL_OCR_TESSDATA_DIR` to another folder containing that file. The
   language data is local and not committed to Git. `LEGAL_OCR_LANGUAGES`
   defaults to `eng`, and `LEGAL_OCR_DPI` defaults to `200`. Without language
   data, native-text pages still extract, but pages requiring OCR receive an
   explicit failure warning rather than being silently treated as blank.
4. Start the extraction worker from the backend directory in another terminal:

   `python -m app.workers.legal_extract --watch`

   Use `--once` to process at most one queued job and exit. The worker
   claims jobs, verifies the original PDF SHA-256 and page count, saves each
   page and its positioned blocks, and records progress, attempts, and final
   output hash. A lost worker lease can be reclaimed on the next run.
5. Run the frontend locally: `npm run dev -- --hostname localhost`.
6. Open `http://localhost:3000/admin/ingest` and fill in the PDF's verified
   title and source details. After upload, choose **Inspect pages** to open
   `http://localhost:3000/admin/ingest/<file_id>`. The screen is unavailable
   in production builds.

The backend route is disabled by default and also requires loopback client and
Host addresses; browser requests must originate from the configured local
frontend origin. This is a **development control**, not a replacement for
authentication if an admin UI is later deployed. Keep the app and private
`uploads/legal` directory on a trusted machine. The `uploads/` tree is ignored
by Git.

## Data and deduplication

- A valid unencrypted PDF can contain scanned or blank pages. The worker
  records one `file_pages` row for every physical page. Native text is used
  where available; image-heavy, scanned, or suspiciously encoded pages are
  routed through OCR. `file_blocks` saves positioned text/image regions.
- The inspector displays the original rendered PDF page beside the saved
  extracted text, warnings, and block overlays. A page with no detectable
  content is a `blank_candidate` needing human review, not an automatically
  verified blank page. OCR results also need review. Page extraction does not
  classify legal headings, sections, tables, or schedules yet.
- `legal_files.sha256` identifies identical bytes. Re-uploading identical
  bytes reuses the stored blob and extract job; another legal edition can
  assign the same file independently.
- The upload form does not infer legal effective dates from the PDF's file
  creation date. Optional dates are stored as unverified metadata.
- The returned `job_status=queued` means the request is recorded. It progresses
  only while the separate worker is running. `completed` means every page has
  an outcome, **not** that every page is correct or approved. Failed pages and
  review warnings stay visible. The old `/ask` corpus remains unchanged.
- A completed or failed job can be requeued from the inspector. Each run
  creates a new extraction record; previous attempts are retained for audit.
- If a PDF is written successfully but the database transaction fails, an
  unreferenced content-addressed blob may remain on disk. It is safe to reuse
  on retry; a later maintenance command can report and clean such orphans.

## First official pilot PDF

The team supplied `https://www.punjablaws.gov.pk/lawspdf//2.pdf`. Its PDF
metadata and text identify it as *The Punjab Murderous Outrages Act, 1867*
(Act XXIII of 1867), under Criminal Law. It has six physical pages, including
one page with no selectable text. Do not equate the PDF creation timestamp or
the Act's historical date with a verified current effective date.

Registered in the development database on 2026-10-05 as edition
`punjab-laws-snapshot-2026-10-05` under jurisdiction `PK-PB` and category
`criminal-law`. Its `legal_files.id` is
`834e10c8-990d-4a1d-82b1-abec590a1a45`, SHA-256 is
`37fa6ca65a2520ea25e74fa9688f47bbf8cca36ab44d00edb7c929c2c033049c`,
and the extraction job ID is `6e9d562d-17bc-4fcd-b245-a0918104bfd7`.
Re-uploading the same bytes returned the same file and job. On 2026-10-06,
extraction `977594b3-e538-4e84-a030-5ea7b0ae9fdf` completed with all six
physical pages accounted for: pages 1 and 3-6 have native text; page 2 has
no extracted text and is flagged `needs_review` / `blank_candidate`. The
original rendering of page 2 appears blank, but no reviewer decision has been
recorded. Open its inspector at
`http://localhost:3000/admin/ingest/834e10c8-990d-4a1d-82b1-abec590a1a45`.

The subsequent draft legal-structure stage is documented in
`LOCAL_LEGAL_STRUCTURE.md`. Extraction alone does not create legal provisions
or make the PDF available to `/ask`.
