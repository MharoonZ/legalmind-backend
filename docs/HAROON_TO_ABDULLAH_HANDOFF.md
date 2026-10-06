# Haroon to Abdullah: draft structure handoff

Haroon's upload, page-extraction, and draft-structure pipeline is implemented.
The contract for Abdullah is **approved structured revisions**, not arbitrary
PDF files and not the legacy 30-section corpus. No revision is approved yet.

## What is available now

`legal_versions` identify editions. Each `corpus_revisions` row belongs to one
edition and has an incrementing `revision_number`. `revision_sources` selects
one or more completed PDF extractions assigned to that edition. `provisions`
contain an ordered parent tree, stable `reference_path`, and each node's own
text (not duplicated in its children). `provision_spans` map exact slices of
own text to `file_blocks` on `file_pages`; the parser's save transaction checks
that both slices are identical. `revision_block_coverage` classifies every
source block. `corpus_revisions.parser_report` contains warnings and counts.
Raw `provision_references` are unresolved textual mentions, not verified law
relationships.

Local read APIs and the inspector are documented in `LOCAL_LEGAL_STRUCTURE.md`.
These endpoints are development-only; Abdullah's production retrieval service
should query the database or a protected publication API, not the local-admin
routes.

## Gate before indexing

Do **not** ingest a revision merely because its structure job says completed.
The future reviewer/approval transaction must verify:

1. Every selected extraction covers all physical pages, with no failed or
   unresolved `needs_review` page; blank and excluded pages have explicit
   reviewer decisions.
2. No substantive block is `partial` or `unmapped`; every ignored header,
   figure, note, table, OCR passage, and parser warning has been accepted or
   corrected by a reviewer. Exact spans are revalidated against stored text.
3. Provision hierarchy, reference paths, ordinals, and own-text boundaries
   are approved. Corrections create a new revision rather than editing a
   previously reviewed one.
4. `review_state='approved'`, `approved_by`, `approved_at`, and content hash are
   set atomically and the approved revision is frozen. This service is **not
   implemented** in Haroon's current stage.

Abdullah can implement chunking/indexing against approved revision fixtures
while that review gate is built, but must keep the publication switch off for
unapproved drafts. For search, retain `revision_id`, `provision_id`, and source
span IDs on every chunk so citations can return to the exact PDF page/block.
Re-indexing should be versioned: a newly approved revision creates a new
publication and never silently changes the evidence behind an old answer.

## Current pilot state

Latest development draft: `e57ca72b-c799-459e-869a-5ab2d1ccfb87`
(revision 3, parser `legal-structure-v3`). It has Sections 1-17, 35 nodes,
43 exact spans, 27 mapped blocks, 5 candidate ignored blocks, and no partial
or unmapped blocks. It is **not approved**. Physical page 2 has no selectable
content and remains `needs_review`; running-header and source-note warnings
also require human decisions. The original PDF renders page 2 as apparently
blank, but that visual observation is not a persisted reviewer decision.
