# LegalMind Phase B: chunking and PostgreSQL keyword search

**Audit/build date:** 13 October 2026. **Scope:** derived data after an approved `corpus_revisions` row. This document describes the running backend and the inspected development database, not a proposed 30-section prototype. It does not authorize embeddings, public QA, or legal publication.

## Supervisor explanation in one minute

An approved legal edition is a frozen transcription of one or more source PDFs. We do not chunk the PDF directly. We read its approved provision tree and exact page/block spans, exclude notes and omitted law, and make source-ordered passages. Each passage retains fragment-level links back to the provision and original PDF page. PostgreSQL automatically turns searchable text into a `tsvector` and maintains a GIN index. A keyword query first selects an approved revision/build, then matches the GIN-indexed vector and ranks the passages. The result includes a stable revision ID, Act title/edition, legal reference path, source span, file hash, page, block, and character ranges for later citation verification.

```text
approved corpus_revision (edition + content hash)
       | provisions + exact provision_spans -> file_blocks/file_pages/legal_files
       v
chunk_sets (one immutable build/config per revision)
       | 1:N
legal_chunks (source-ordered passage + search_text + generated search_vector)
       | 1:N                                  | GIN full-text index
legal_chunk_provisions (ordered exact fragments)        |
       | source_span_id + offsets                         v
       +------------------------------> scoped keyword query -> ranked results
```

The source PDF, extraction, structure parser, approval state, and spans were **not changed** in this work.

## The actual three tables

| Table | Identity and relationships | Purpose |
|---|---|---|
| `chunk_sets` | UUID PK; FK `revision_id -> corpus_revisions`; unique `(revision_id, content_sha256, chunker_version, config_sha256)` | Immutable derived build pinned to an approved structure and config. A revised edition or new chunker creates another set, never mixes rows into the old one. `chunk_report` records counts/warnings. |
| `legal_chunks` | UUID PK; composite FK `(chunk_set_id, revision_id) -> chunk_sets`; composite FK `(root_provision_id, revision_id) -> provisions`; unique `(chunk_set_id, ordinal)` and `(chunk_set_id, chunk_key)` | One root Section/Rule/Article/Schedule/Preamble or ordered part of it. Stores display prefix, body, legal path, SHA-256, character count, `search_text`, and DB-generated `search_vector`. GIN on `search_vector`; btree on `(chunk_set_id,label)`. |
| `legal_chunk_provisions` | V2 PK `(chunk_id, ordinal)`; composite FK to same-revision chunk/provision and `(source_span_id, provision_id, revision_id) -> provision_spans` | Ordered fragment ledger. The same parent provision can occur before and after its child list. Each v2 row records source-span ID and character ranges in both provision and chunk. Historical v1 rows have null fragment columns and remain available for comparison, **not current retrieval**. |

The migration `20261013_15` only extends the third table and its PK. It retains every v1 row. The actual database was on Alembic `20261010_14` before and `20261013_15` after migration. No new Phase A tables were created.
Supporting indexes are `ix_chunk_sets_revision_id`, `ix_legal_chunks_chunk_set_id`, `ix_legal_chunks_root_provision_id`, `ix_legal_chunks_chunk_set_label`, `ix_legal_chunks_search_vector` (GIN), `ix_legal_chunk_provisions_provision_id`, and the new `ix_legal_chunk_provisions_source_span_id`. A unique `(chunk_id, ordinal)` constraint also remains alongside the v2 PK (redundant but compatible with historical migration 13).

## Generation policy and why v2 was needed

The old builder concatenated a parent's entire `own_text` before all children. In 18 saved v1 parent/child cases, exact source positions show parent continuation after a child, so the passage order was wrong despite valid character slices. V2 orders exact `provision_spans` by physical source location, preserving a parent's opening, list children, and later continuation in that order. It retains whitespace-only gaps in the provision text and refuses uncovered non-whitespace, overlapping ranges, ambiguous multiple-source ordering, non-exact mappings, or a block/provision slice mismatch. It does not rewrite the approved text.

Roots are Preambles, Sections, Rules, Articles, and Schedules. `other` nodes (contents, source/amendment notes, headers, etc.) and omitted/repealed/deleted nodes plus their descendants are excluded. Part/Chapter headings are context containers, not standalone operative chunks. If a future Part/Chapter stores substantive standalone law, that needs a review decision; do not silently index it as a Section. There is no source-text overlap between chunks. Display title/reference prefixes are repeated on continuation chunks, but they are not claimed as source text. A root may be split at exact source-fragment boundaries under a **soft 4,000-character** budget; oversized fragments are warned about, never truncated. The present 23 v2 sets have zero >4,000-character chunks.

**Important tokenizer limit:** character length is *not* a model token count. No embedding model/tokenizer is selected in this stage, so `chunk_report` explicitly records `size_unit: characters` and `tokenizer: null`. Before embeddings, choose and pin a tokenizer/model/input recipe, reserve prompt overhead, measure actual tokens, and create a new versioned build if the passages need different boundaries. Do not call these 4,000-token chunks.

Builds run in one database transaction per revision. A retry reuses the unique build fingerprint and now checks expected chunk/fragment counts; it cannot leave a half-written set appearing complete after a process failure. IDs are generated once and reused on rerun, while text/order/keys/hashes are deterministic for identical inputs. Historical revisions and v1 sets remain unchanged.

## Keyword search behavior

`search_text` contains Act title, root identifier, heading, and the chunk body. Because the body can also print its heading, some heading repetition is deliberate search context, not extra law. The current 23 `legal_instruments.aliases` arrays are all empty; aliases and `citation_key` are **not yet** included in `search_text`. Before cataloguing aliases for a larger corpus, version the search recipe and rebuild rather than silently changing text under the same build fingerprint. `search_vector` is a PostgreSQL **stored generated** column using `to_tsvector('english', search_text)`: inserts/updates regenerate it automatically. `ix_legal_chunks_search_vector` is a real GIN index. The normal `EXPLAIN (ANALYZE, BUFFERS)` plan for `consumer courts` used a Bitmap Index Scan on that index (28 index matches, 20 returned, about 0.09 ms server execution in this small warm-cache check). This is not an end-to-end API latency measurement.

The repository uses parameterized `plainto_tsquery('english', query)` (AND between surviving terms) and ranks matching chunks by indexed-text score, with extra title and heading scores for a selected corpus. English stemming and stop-word removal apply; punctuation/quotes do **not** provide strict phrase semantics through `plainto_tsquery`. An empty query returns no results, limit is capped at 100, ties have deterministic order, and corpus results can collapse multiple parts of one root. The caller supplies approved **revision IDs** as the corpus selection; users need not choose a category. A result is accepted only when its revision is approved, its pinned content hash still matches, and its chunker/config fingerprint is v2. Direct one-set lookup also refuses historical v1 sets. A public publication/edition-selection API does **not** exist yet, so this is a repository/CLI retrieval capability, not a production user search endpoint.

Exact citations should **not** be inferred from stemming. `find_by_label` compares a root label by equality within one selected chunk set and accepts an explicit kind such as `section` versus `rule`; `find_by_reference_path` compares a stored path exactly for a child such as paragraph `(da)`. The caller must first resolve the correct Act and edition. The repository does not guess that `19` means `19A`, or that a reference to another Act is a reference to the current Act. English stemming/stop words/punctuation and AND semantics can miss a concept or misspelling; this stage does not silently rewrite the user's question.

## What was actually checked in the database

| Check | Before (v1) | After (v2, retained alongside v1) |
|---|---:|---:|
| Approved revisions / provision rows | 23 / 2,815 | unchanged |
| Chunk sets | 23 | 46 total: 23 v1 + 23 v2 |
| Chunks | 529 v1 | 531 v2 (1,060 total) |
| Membership/fragment links | 1,824 v1 | 2,473 v2 (4,297 total) |
| Confirmed parent-continuation misorders | 18 v1 cases | 0 flagged in v2 source-order check |
| Exact approved span-to-block text mismatches | 0 | 0 |
| V2 missing eligible provisions, fragment text mismatches, wrong span links, range overlap, source-order violations | n/a | 0 flagged |
| V2 excluded-note/historical links, missing provision hashes, empty chunks, >4,000-character chunks, duplicate exact chunk texts, vector drift | n/a | 0 |

In 191 v2 chunks the same provision legitimately has multiple fragment rows (often multiple source blocks, sometimes parent text around children). The new `(chunk_id, ordinal)` key permits this without duplicating source characters; the coverage audit checks for overlap.
The live search check also confirmed that handing a historical v1 set ID to the current lookup returns no results.

The original approved spans had 20 parent-after-child physical patterns; 18 produced provable v1 order errors under its parent-first membership. The v2 audit found no flagged defect in the checks above. These are **automated DB/source-text comparisons**; this task did **not** visually inspect every PDF page or prove legal interpretation correct. The original PDFs and approved structure remain the upstream authority.

### Search evaluation (measured, small and deliberately not a production benchmark)

Run `python -m scripts.evaluate_keyword_search_readonly` to see every query and its actual top hits. On the 23 approved v2 revisions, the first 6 title/phrase queries returned the expected Act at rank 1 after title/heading boosting (before boosting, `probational release` was rank 2). Four body/concept queries were added; 3 found the expected Act at rank 1, while `begging children` returned nothing. Overall: **9/10 known-target queries at top 1 and in top 5; 2/2 deliberate no-result queries returned nothing.** These labels are Act-level judgments, not independently graded passage-level relevance or legal correctness. `gambing` demonstrates a misspelling gap, not a successful correction. Exact lookup also distinguished Section `2` from `2A` within one Act and resolved paragraph `(da)` with source refs. No same-Act `19`/`19A` pair was present in this corpus, so that particular live pair was not measured. Do not infer general recall/precision from this tiny set.

## Runbook (PowerShell, from `legalmind-backend`)

Use the configured development database and keep credentials in `.env` (ignored by Git). Make a full managed PostgreSQL backup/snapshot before production migration; the local JSON snapshot below protects only the three derived tables.

```powershell
cd C:\Users\Admin\Desktop\FYP\legalmind-backend
.\.venv\Scripts\python -m scripts.backup_legal_chunks
.\.venv\Scripts\python -m alembic current
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m scripts.preview_legal_chunks_readonly
.\.venv\Scripts\python -m scripts.build_legal_chunks
.\.venv\Scripts\python -m scripts.audit_legal_chunks_readonly --summary --gate
.\.venv\Scripts\python -m scripts.evaluate_keyword_search_readonly --gate
.\.venv\Scripts\python -m scripts.check_keyword_index_readonly
.\.venv\Scripts\python -m pytest -q
```

For one approved revision, append `--revision-id <UUID>` to the build or audit command. The audit `--gate` exits 2 when a v2 integrity/vector/index check fails; historical v1 ordering issues remain visible but do not make the v2 gate fail. The evaluation `--gate` protects the established title/reference/v1-isolation regressions; it still reports the known `begging children` miss. `build_legal_chunks --dry-run` reports existing sets but does **not** preview content; use the preview script for a real read-only build test. On 13 October the ignored local snapshot was `backups/legal_chunks_20261013T171501Z.json`, SHA-256 `e8004295bbdc854d1df7dd5360aecd173a282cec03cc0eca3d83e75dd401062c`, with 23/529/1,824 v1 rows. Keep that file private; it is not committed or a substitute for a whole-DB backup.

## Seven practical checks and failure actions

The read-only audit script implements the cross-row checks; use its JSON `issues` as a **gate**, not just an informational log. It currently reports the retained v1 misorders; a new `v2_*` issue is a blocker. These SQL snippets provide independent spot checks in `psql` or Supabase SQL editor.

1. **Every approved revision has exactly one v2 set with a matching hash:**

   ```sql
   SELECT r.id, count(s.id) AS v2_sets
   FROM corpus_revisions r LEFT JOIN chunk_sets s
     ON s.revision_id=r.id AND s.chunker_version='legal-chunk-v2-source-order'
    AND s.content_sha256=r.content_sha256
   WHERE r.review_state='approved' GROUP BY r.id HAVING count(s.id)<>1;
   ```

   Expected now: zero rows. A missing set means rebuild that revision; >1 means inspect config versions, not delete arbitrarily.

2. **Text loss/duplication/order:** run `python -m scripts.audit_legal_chunks_readonly --summary`. Expected v2 missing/overlapping coverage, fragment mismatch, and source-order issues: zero. A failure means stop use of that set and inspect its `legal_chunk_provisions` ranges against `provision_spans` and `provisions.own_text`. Valid offsets alone do not prove legal parent ownership.

3. **Notes/historical text excluded:**

   ```sql
   SELECT count(*) FROM legal_chunk_provisions l
   JOIN legal_chunks c ON c.id=l.chunk_id JOIN chunk_sets s ON s.id=c.chunk_set_id
   JOIN provisions p ON p.id=l.provision_id
   WHERE s.chunker_version='legal-chunk-v2-source-order'
     AND (p.kind='other' OR p.source_status IN ('omitted','repealed','deleted'));
   ```

   Expected: `0`. If nonzero, do not use that set in search. Ancestor exclusions are also checked by the builder/auditor.

4. **A hit points to the right PDF/page/exact text:** take a returned `source_span_id` and inspect:

   ```sql
   SELECT lf.original_filename, lf.sha256 AS pdf_sha256, fp.page_index,
          b.block_ordinal, ps.block_char_start, ps.block_char_end,
          substring(b.normalized_text FROM ps.block_char_start+1
                    FOR ps.block_char_end-ps.block_char_start) AS exact_source,
          substring(p.own_text FROM ps.provision_char_start+1
                    FOR ps.provision_char_end-ps.provision_char_start) AS saved_provision
   FROM provision_spans ps JOIN provisions p ON p.id=ps.provision_id
   JOIN file_pages fp ON fp.id=ps.page_id JOIN file_blocks b ON b.id=ps.block_id
   JOIN file_extractions fe ON fe.id=ps.extraction_id
   JOIN legal_files lf ON lf.id=fe.file_id
   WHERE ps.id='<source-span-uuid>';
   ```

   Expected: two text columns identical, and the filename/hash/page match the source PDF. The audit checks every exact saved span automatically; opening the PDF page is still the human visual step.

5. **`search_text`, generated vector, and GIN:** `audit_legal_chunks_readonly --summary` expects `bad_vectors: 0`, both named indexes, and no empty chunks. `check_keyword_index_readonly` must show a `Bitmap Index Scan on ix_legal_chunks_search_vector` for its sample query. If the planner later chooses a sequential scan on a very small table, check the forced plan and row estimates before changing indexes.

6. **Cross-Act relevance/scoping:** run `evaluate_keyword_search_readonly`; inspect actual title, edition, reference path, source refs and expected result. A correct SQL match is not a correct legal answer. Exact-reference lookup must first select the intended Act/edition; the current repository does not decide which legal edition is currently applicable.

7. **Safe retry:** rerun `python -m scripts.build_legal_chunks --revision-id <UUID>`. Expected `already_built`, the same `chunk_set_id`, and no row-count growth. If it says `created`, check the revision hash, version and config hash; if it reports incomplete, audit/restore rather than deleting rows. A failed build rolls back its per-revision transaction.

## Safe handling and remaining decisions

The schema migration was additive to live text: it changed the fragment ledger key, added nullable fields and constraints, then 23 **new** v2 sets were inserted. The 23 v1 sets and all source/approved rows remain intact. Roll back application selection to v1 only as a temporary operational fallback **with its known misordering**, while keeping migration 15 and v2 history. The Alembic downgrade deliberately refuses while v2 fragment rows exist; a full rollback needs a database restore or a separately reviewed, backed-up retirement procedure. Do not manually truncate these tables.

Not production-ready yet: no published-corpus/edition-selection policy or public search API; no embedding tokenizer/token budget; alias/citation-key search recipe still needs versioning when those fields are populated; no typo normalization or semantic retrieval; `plainto_tsquery` can miss related wording (`begging children`); title-heavy queries can fill top results with sections from one Act; no deep pagination, formal passage-level relevance set, or concurrency/load benchmark for 100+ PDFs. None of those limitations justify altering approved extraction or structure in this Phase B task. Embeddings, vector search, reranking, and answer generation belong to later stages and must use the pinned v2 revision/build and its source refs.
