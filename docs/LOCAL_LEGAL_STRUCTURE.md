# Haroon step 3: draft legal structure and source spans

This stage consumes the **stored extraction**, not PDF text scraped again and
not the legacy `/ask` corpus. It can build a new draft from one or more PDFs
assigned to the same `legal_versions` row. It does not approve, chunk, embed,
index, or publish the draft. Abdullah must not consume a draft as authoritative.

## Local operation

1. Apply Alembic revision `20261006_06` (`python -m alembic upgrade head`). It
   adds `corpus_revisions.parser_report` and `revision_block_coverage` so that
   parser warnings and each block's mapped/ignored/partial/unmapped decision
   are persisted. It does not alter legacy search tables.
2. Keep the local-only backend and frontend running as described in
   `LOCAL_LEGAL_UPLOAD.md`. Start a separate structure worker from the backend
   directory: `python -m app.workers.legal_structure --watch`.
3. Open `/admin/ingest`, then **Build draft** on an uploaded edition. The
   source-picker lists assigned PDFs and their latest completed extractions.
   It also links existing draft revisions. Select one or more sources.
   Omitting an assigned PDF is recorded as a warning.
4. The API creates `corpus_revisions(review_state='draft')`, matching
   `revision_sources`, and a durable `processing_jobs(task_type='structure')`
   row. It does not parse inside the POST request. A new parse creates the next
   revision number; it never overwrites an earlier parsed revision. The worker
   claims a queued job with a lease/attempt record and saves the entire parse
   atomically. A failed unparsed draft can be retried. A crashed worker's
   expired lease is reclaimable.
5. Inspect the revision tree, its own text, exact source spans, warnings, and
   block coverage in `/admin/ingest/revisions/<revision_id>`. A source-span
   link opens the original PDF page and selected block in the page inspector.

## Source and reference convention

- The source list comes from `legal_file_assignments` and completed
  `file_extractions`. It is ordered primary, supplement, amendment, then file
  ID. The same PDF cannot contribute two extraction attempts to one draft.
- Every `reference_path` begins with `f:<legal_files.id>` so two PDFs can both
  contain Section 1 without collision. A section is `/s:19A`, Rule is
  `/r:7`, Schedule is `/sch:I`, Part is `/part:II`, Chapter is `/ch:III`,
  subsection is `/sub:1`, and lettered paragraph is `/p:a`. Parent paths are
  included, e.g. `f:<id>/ch:II/s:19A/sub:1/p:a`. Repeated identifiers receive
  `/duplicate:2` etc. plus a review warning, never a silent overwrite.
- Text in `provisions.own_text` is copied from exact slices of the stored
  `file_blocks.normalized_text`. Parent text excludes child text. Each copied
  slice has an exact `provision_spans` row with both provision and block
  character offsets. The save transaction verifies the two slices match and
  rejects overlapping source ranges. Separators inserted between slices are
  not falsely attributed to the PDF.
- The deterministic parser recognizes line-start Part/Chapter, Sections,
  Rules, Articles, Schedules, Preamble, and nested `(1)`/`(a)` labels. It
  preserves combined labels such as `19(1)(a)` as printed; it does not truncate
  `19A` to `19`. Non-heading text before a recognized provision, table blocks,
  and source notes are retained as `other` nodes with review warnings where
  needed. Repeated running headers and image/empty regions are explicitly
  classified as ignored. All blocks get a coverage row.
- `provision_references` stores raw textual mentions as **unresolved** only.
  This stage does not infer legal effect or cross-law relationships.

## Important limits and review gate

This is a conservative draft parser, not a universal legal-layout recognizer.
Its table, footnote, header, OCR, and heading-boundary rules are heuristics.
Every page with `needs_review`/`failed`, every ignored candidate header,
ambiguous source note, noisy heading, partially mapped block, or unmapped
block must be assessed in a later reviewer-decision stage. `completed` means
the draft job finished, not that its contents are correct. No UI action in
this stage can set `review_state='approved'`.

For large corpora, create a revision **per legal edition**, not one revision
containing all 40 criminal-law Acts. Each edition may have multiple source
PDFs. The administrative catalogue, durable jobs, and edition-scoped paths do
not impose a 30-section or one-PDF limit. The current worker materializes all
blocks for one edition in memory and commits one atomic result; benchmark and
add streaming/batching before feeding unusually large multi-thousand-page
editions. The provision tree endpoint returns all nodes; pagination may be
needed for very large editions.

## Local API

All paths below are under `/local-admin/legal` and require the same
development-only loopback guard as upload/extraction:

| Method | Path | Purpose |
|---|---|---|
| GET | `/versions/{version_id}/structure-sources` | Assigned PDFs and latest completed extraction per file |
| GET | `/versions/{version_id}/revisions` | Existing draft/review revisions for reopening |
| POST | `/versions/{version_id}/revisions` | Create draft + structure job; optional `{"extraction_ids":[...]}` |
| GET | `/revisions/{revision_id}` | Job, sources, provision tree, warnings and summary |
| GET | `/revisions/{revision_id}/provisions/{provision_id}` | Own text, exact spans, unresolved references |
| GET | `/revisions/{revision_id}/coverage?status=unmapped&limit=100&offset=0` | Per-block coverage report |
| POST | `/revisions/{revision_id}/retry` | Requeue failed, still-unparsed draft |

## Pilot

The development pilot *Punjab Murderous Outrages Act, 1867* has six physical
pages. The latest draft revision `e57ca72b-c799-459e-869a-5ab2d1ccfb87`
(`legal-structure-v3`) parsed Sections 1-17, with 35 total nodes (including
preamble/contents/source-note nodes), 43 exact spans, 27 mapped blocks, 5
intentionally ignored blocks,
and zero partial/unmapped blocks. Six raw cross-reference mentions remain
unresolved. Its page 2 is still `needs_review` / `blank_candidate`. The
amendment-note and unclassified-text warnings also need human inspection.
Section 4 was checked against the rendered original on pages 3 and 4; its
stored spans reproduce the source substrings exactly.
Revision 2 was used to test expired-lease recovery: a second attempt reclaimed
the job and completed without overwriting revision 1. Revision 3 tested retry:
a simulated first-attempt failure could be requeued once, a second immediate
retry was rejected, and a completed job could not be requeued. Its second
attempt completed. Both attempt histories remain recorded.
