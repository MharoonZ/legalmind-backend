# Local Act/Rules PDF upload (Haroon, step 1)

The backend now supports registering many Acts/Rules PDFs, one upload at a
time. It validates the original PDF, stores it under a SHA-256-derived path,
records jurisdiction/category/instrument/edition/file assignment, and creates
one queued extraction job per unique PDF. It does **not** extract pages or make
the uploaded Act searchable yet.

## Run locally

1. Confirm the database is at migration `20261005_05` or later.
2. Set `LEGAL_INGEST_ENABLED=true` in the backend's local `.env`. Do not use
   this on a public server. Bind the backend to `127.0.0.1`, not `0.0.0.0`:

   `uvicorn app.main:app --host 127.0.0.1 --port 8000`

3. Run the frontend locally: `npm run dev -- --hostname localhost`.
4. Open `http://localhost:3000/admin/ingest` and fill in the PDF's verified
   title and source details. The screen is unavailable in production builds.

The backend route is disabled by default and also requires loopback client and
Host addresses; browser requests must originate from the configured local
frontend origin. This is a **development control**, not a replacement for
authentication if an admin UI is later deployed. Keep the app and private
`uploads/legal` directory on a trusted machine. The `uploads/` tree is ignored
by Git.

## Data and deduplication

- A valid unencrypted PDF can contain scanned or blank pages. OCR routing and
  page accounting are the **next** implementation step.
- `legal_files.sha256` identifies identical bytes. Re-uploading identical
  bytes reuses the stored blob and extract job; another legal edition can
  assign the same file independently.
- The upload form does not infer legal effective dates from the PDF's file
  creation date. Optional dates are stored as unverified metadata.
- The returned `job_status=queued` means the request is recorded. No worker
  consumes it yet. The old `/ask` corpus remains unchanged.
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
and the extraction job ID is `6e9d562d-17bc-4fcd-b245-a0918104bfd7`
(`queued`). Re-uploading the same bytes returned the same file and job.
