# Reviewed legal-PDF extraction examples

These source-hash-bound JSON manifests document visual corrections without altering the PDF or any earlier extraction attempt. The extraction worker accepts `--review-manifest <path>` when a matching file's extraction job is queued. A hash mismatch fails the job; it must not apply edits to another PDF.

- `112-goondas-schedules.json` marks three inspected table regions on PDF pages 11-12. The worker splits numbered section/punishment row groups into 28 First Schedule entries (8 on page 11 and 20 on page 12) and retains four Second Schedule entries. Each split stores an exact character slice into its original detected cell. The continued Chapter XI label on page 12 is recorded explicitly.
- `393-walls-glyph.json` changes one normalized text-layer sequence, `(l) This Act may be called`, to the visually printed `(1) This Act may be called` on PDF page 3. The replacement is length-preserving, so page/block offsets remain exact. Raw page/block text keeps `(l)`.

The corresponding completed reviewed extraction IDs are `bf134ea8-8431-4287-87d2-8fee6fb25d7a` (`112.pdf`) and `ecbba9c6-8c3b-4d7f-9a34-f06ae756b9e5` (`393.pdf`). Their latest completed, unapproved structure revision IDs are `69df7341-b29d-4d3a-bd49-83bff7f4fa42` and `010799a8-b013-4bfc-8c87-0a16221b3acc`, respectively. The old attempts and revisions remain available.

The algorithm refuses a table whose item counts do not align or whose reviewed atomic rows fail exact source-cell coverage. A human should still inspect any document before legal approval or indexing; these are agent-assisted visual corrections, not legal certification.

## Future PDFs

These JSON files are examples, not filename-specific parser rules. Every new
upload goes through the same table detection and candidate-row splitting. It
handles aligned numbered entries in tables with two or more columns, preserves
source-cell character offsets, and leaves rows with missing or conflicting
boundaries as `candidate_requires_review` with an `atomization_error`. The
audit blocks approval when such a table remains unreviewed. A reviewer can use
the same `review_tables` manifest format for any registered PDF; the original
detected cells remain available in the inspector.

Possible subsection `(1)` text-layer errors, such as `(l)` or `(I)`, are
detected on future pages and shown as `ambiguous_numbering_glyph`. They are
**not** automatically replaced: the letter may be legally correct. Compare
the PDF image, then use a length-preserving `text_replacements` entry with the
source block ordinal. Uncorrected candidates remain blocking even if another
candidate on the same page was corrected. If `(l)` really is printed as a
letter, use `confirm_numbering_glyph_blocks` with the reviewed block ordinal
after checking every ambiguous marker in that block; that leaves its text
unchanged and records the visual decision. Raw text
remains unchanged in either case.

Run a source-hash-bound manifest with
`python -m app.workers.legal_extract --once --review-manifest <path>` after
requeuing that file's completed extraction job. The worker claims only a
queued PDF whose hash matches the manifest; it will not consume an unrelated
queued file. Build a new structure revision from the resulting completed
extraction and inspect its audit before approval. Keep any other extraction
watcher stopped during a reviewed retry, since a normal worker has no
knowledge of a command-line manifest and could claim the job first.
