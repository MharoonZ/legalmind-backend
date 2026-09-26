# Sprint 2 - Upload stage (Abdullah, Week 1)

## What this delivers
`POST /contracts` - upload one contract PDF, optionally with a chat question.
`GET /contracts/{document_id}` - check its status.

`/ask` is untouched - still plain JSON, text-only, exactly like Sprint 1.

## What happens on upload
1. File is checked: really a PDF, ≤10 pages, ≤10 MB, not encrypted, has
   selectable text (not a scan).
2. Saved to disk under `UPLOAD_DIR` (default `uploads/contracts/`), named by
   a generated id - never the user's original filename.
3. A `documents` row (file identity) and a `document_versions` row
   (`status="uploaded"`) are created in Postgres.
4. The question (if any) is stored on the version row for later - it is not
   answered yet.

## What Haroon needs for extraction
- Read the file from `documents.storage_path` (or re-derive it: it's saved
  as `<upload_dir>/<document_id>.pdf`).
- Work against `document_versions.id` (the `version_id` from the response) -
  that's the row his extraction step should update.
- `document_versions.status` already has the values he'll move through:
  `uploaded -> extracting -> extracted` (or `extraction_failed`, with a
  message in `document_versions.error_message`).
- His clause/citation tables are his own design (per the sprint doc) - they
  just need a foreign key to `document_versions.id`.

## What's still missing (by design, not yet in scope)
- No extraction, no clauses, no summary. `GET /contracts/{id}/summary` from
  the sprint plan is not built yet - it depends on Haroon's tables + the
  Week 2 AI step.
- No page-count/OCR handling for scanned PDFs beyond "reject them" (the
  no-text-layer check here is a quick presence check, not real extraction).

## To run it
```
alembic upgrade head        # creates documents + document_versions tables
pip install -r requirements.txt   # adds python-multipart
uvicorn app.main:app --reload
```
Try it at `http://127.0.0.1:8000/docs` under the `contracts` tag.

## Tests
`tests/test_document_upload.py` - validation logic, no DB needed.
`tests/test_contracts.py` - the two endpoints, with a fake in-memory
repository (same pattern as `tests/test_ask.py`).
All 95 tests pass (`pytest`).
