# Haroon's legal-corpus schema (first implementation slice)

This is the table reference for the legal corpus that Phase A writes (upload,
extraction, structure, review). It was written for the first implementation
slice; the pipeline that now fills these tables, and the later chunk,
embedding and publication tables, are described in
[ARCHITECTURE.md](ARCHITECTURE.md) and the [README](../README.md). Private
uploaded contracts are never mixed into the authoritative legal corpus.

## Table ownership and meaning

| Area | Tables | Identity |
|---|---|---|
| Reviewer identity | `app_users` | External authentication subject; no password copy |
| Registry | `jurisdictions`, `categories`, `legal_instruments`, `instrument_categories`, `legal_versions`, `instrument_relations` | Which law, where, in what edition, and related to which other law |
| Source extraction | `legal_files`, `legal_file_assignments`, `file_extractions`, `file_pages`, `file_blocks` | PDF bytes, upload-time law/edition assignment, and a reproducible, page-by-page extraction output |
| Reviewed legal text | `corpus_revisions`, `revision_sources`, `provisions`, `provision_spans`, `provision_references`, `review_events` | One parsed/reviewed representation, traced to PDF pages and blocks |
| Durable work | `processing_jobs`, `job_attempts` | Task progress, retry attempts, and restart recovery |

`legal_instruments` is the stable Act/Rules identity. `legal_versions` is a
legal edition. `corpus_revisions` is a correction or reparse of that edition.
`file_extractions` is a run of a PDF extraction configuration. These are not
interchangeable. A single PDF may supply more than one revision, and a revision
may use more than one PDF, through `revision_sources`.
`legal_file_assignments` preserves the selected edition immediately at upload;
composite foreign keys make each later `revision_sources` row agree with that
assignment, the extracted PDF, and the revision's edition.

## Constraints enforced by PostgreSQL

- Every foreign key is typed; there are no polymorphic string IDs. Source
  pages/blocks and provision spans use composite foreign keys to prevent a
  provision from citing a page in an unrelated extraction or revision.
- Uniqueness is scoped: `(jurisdiction_id, citation_key)` for an instrument,
  `(instrument_id, edition_key)` for an edition,
  `(legal_version_id, revision_number)` for a corpus revision,
  `(extraction_id, page_index)` for a physical page, and
  `(revision_id, reference_path)` for a legal reference.
- `provisions.label` and `reference_path` are text, not integer section
  numbers. The database accepts `19A`, `19(1)(a)`, Rules, and Schedules.
- Page outcomes, extraction methods, OCR confidence, positive sizes/indexes,
  effective-date order, character-range ordering, and approval metadata have
  checks. An excluded page requires a non-empty reason. An exact provision
  span requires a block and block offsets.
- Review events have exactly one base target (extraction or revision), with
  optionally scoped page/block targets. Jobs likewise target exactly one file,
  extraction, or revision. A database trigger rejects edits/deletes to review
  events. Attempts are unique per `(job_id, attempt_number)`.
- Source PDF bytes are referenced by `object_key` and SHA-256. Do not store
  hundreds of PDF binaries in PostgreSQL rows. `legal_files.sha256` deduplicates
  identical bytes, but the hash is **not** a law identifier.

## Checks the schema alone does not prove

The admin approval transaction must check all of these **before** setting a
revision to `approved`. They require counting or inspecting rows across tables;
a normal row `CHECK` constraint cannot establish them:

1. Each selected extraction is complete. Its page rows cover exactly
   `1..legal_files.page_count`, with no gaps, failed pages, or `needs_review`
   pages. Blank/excluded pages need actual reviewer decisions.
2. OCR warnings, tables, schedules, notes, and unmapped blocks are presented
   to a reviewer and explicitly resolved or accepted. Page accounting alone
   does not prove transcription accuracy.
3. Every provision with substantive text has source spans. Each span's offsets
   fit its provision and source block's **stored normalized text**; a database
   cross-table `CHECK` cannot verify those lengths.
4. The provision hierarchy is acyclic. Self-parenting is blocked now, but
   longer cycles require application/transaction validation.
5. The computed content hash covers a specified, canonical serialization of
   the approved tree and spans. Once approved, extraction/revision content
   should be frozen; corrections create new extraction/revision IDs.
6. Reviewer authorization must be checked by the future API/auth layer.
   `app_users.role` alone is not authentication or authorization.

The future approval service should lock the revision and source rows, perform
these checks in one transaction, append a `review_events` row, then update the
revision's approval fields. Abdullah should consume only such approved
revisions. Until that service exists, the database `review_state` is metadata,
**not proof that the complete-PDF gate ran**.

## Migration and handoff

Migration: `20261005_05_haroon_legal_corpus.py`, following `20260927_04`.
It was applied to the configured development Supabase database on 2026-10-05.
The local-only upload flow registered one pilot Act there. The legacy search
path remains active after migration; a later migration and application change will create
Abdullah's chunks/index/publication tables and switch search deliberately.
