# LegalMind database data quality audit

**11 November 2026** · Configured Supabase PostgreSQL · Alembic
`20261016_17` · pgvector `0.8.2`

The audit read the configured database using the application's connection
settings. It compared persisted data with locally available originals and
queried indexes, constraints, roles and execution plans. Corpus and
publication rows were read-only throughout.

## Inventory

| Area | Live rows and scope |
| --- | --- |
| Source catalog | 23 instruments, 23 legal versions, 23 file assignments and 23 original files |
| Extraction history | 29 extractions, 241 page rows and 1,912 blocks |
| Review history | 87 revisions and 87 pinned sources, with 23 approved and 64 drafts. 40 review events, 110 jobs and 118 job attempts |
| Structure history | 12,268 provisions, 16,992 source spans, 7,134 block coverage rows and 2,519 provision references |
| Approved structures | 2,815 provisions, including 516 sections, 683 subsections, 648 paragraphs, 8 schedules, 17 preambles, 24 chapters, 23 parts and 896 other nodes |
| Chunk history | 46 chunk sets, 1,060 chunks and 4,297 chunk/provision links. 529 v1 chunks and 531 v2 chunks |
| Published data | One active release, `legalmind-pilot-20261016` (`3181ffc8-fa34-474a-a531-156928817d35`), with 23 items, 526 roots and 531 chunks |
| Embeddings | One profile, 531 rows. Every active chunk has one BGE-M3 vector. Historical v1 chunks have no active-profile embeddings |
| Private workflow | 2 documents, 2 versions, 4 contract pages, 3 clauses and 43 contract spans. Counts only were inspected |
| Optional answer audit | `qa_runs`, `retrieval_hits`, `answer_claims` and `answer_evidence` are absent at the shared DB's current revision |

## Source and provision integrity

- All **23 SHA-256 hashes** matched existing PDF copies from the local source
  backup. PDF and registered page totals both came to **199**. The
  checkout's default `uploads/legal` path lacked the original set, so the
  comparison pointed `LEGAL_UPLOAD_DIR` at the existing backup for that run.
- The approved extraction set contained **181 extracted pages** and **18
  pages marked needs review**. The auditor re-read all 199 physical pages.
  The extraction methods recorded 182 native pages and 17 `none` pages.
  Warnings included 17 blank candidates, one ambiguous numbering glyph, two
  reviewed table-row cases and one visually corrected glyph. Page outcomes
  are historical extraction flags, separate from review events and the
  approved revision decision.
- There were **zero page-index gaps** across completed extractions and **zero
  differences** reported by the persisted-PDF audit for page counts, native
  raw text, block order, page/block offsets and provision/source text.
- Across revision history the SQL audit found **zero orphan or cross-revision
  provision parents** and **zero exact span mismatches** in provision text,
  block text or page/extraction identity.
- There were **82 repeated sibling-label groups** in the complete historical
  inventory. Some repeated labels can arise from layout or contextual
  numbering. The count is a review lead. A separate check found **zero
  duplicate numbered root groups** within one active instrument and kind.
  Version and reference path stay part of the identity, and exact lookup asks
  for clarification where multiple published paths match.
- Approved nodes include 11 repealed, 9 omitted and 5 deleted source markers.
  The chunker excludes non-operative material and its descendants. A separate
  legal review is needed to certify whether every provision is interpreted,
  labeled and currently applicable. All **23 active editions** currently
  record applicability as `unknown`.
- The active catalog contains the Punjab Consumer Protection Act 2005 and
  lacks Contract Act 1872. Section 19 of the former concerns receipts.
  Questions about fraud or coerced consent cannot use that section as evidence.

## Chunk and vector checks

The chunk audit read all 1,060 stored chunks and checked ordered membership
and source spans against each approved revision.

| Check | Result |
| --- | --- |
| Historical v1 parent continuation out of order | **18 confirmed**. These sets remain historical. |
| Active v2 overlapping or missing mapped coverage, wrong identity or bad sequence | **0** |
| V2 empty chunks, generated FTS vector mismatches, excluded links or duplicate text groups | **0** |
| V2 chunks over the 4,000-character soft limit | **0**. Maximum stored chunk length 3,969 characters. |
| Chunk groups split across multiple parts | **4**. Maximum 3 parts in a root. |
| Source fragment ordering | 20 source-interleaved parent patterns observed. V2 preserves physical order. 191 v2 chunks contain multiple fragments of the same provision, which is distinct from repeated text. |
| Vector dimensions, non-finite/invalid norm, stale input hash, orphan vectors | 531 vectors with dimension 1,024 and **0 flagged issues**. L2 norm tolerance 0.001. |
| Token checks | All 531 inputs counted with the BGE tokenizer including special tokens. Mean 208.45, median 161, P95 539, P99 862 and max 1,016 of 8,192. Zero oversized inputs. The encoder checks the entire input before inference. |

Production chunks have no intentional text overlap. Display headings and
titles repeat for navigation and retrieval. The v2 mapping checks compare
chunk offsets with provision and original normalized block offsets. They
permit only whitespace gaps between mapped source fragments. The profile
stores the model identifier and input recipe, with a known provenance gap
for the original model weights commit.

## Access, indexes and plans

- All **31 audited existing tables** have RLS enabled and zero policies.
  The server role has `BYPASSRLS`. The catalog showed table-level grants for
  `anon`, `authenticated` and `service_role`. Read-only tests under
  `anon` and `authenticated` each saw **zero statutory chunks and zero
  contract versions**. Hosted contract ownership remains a prerequisite
  for private uploads over the public API.
- Those tables have **55 validated foreign keys** and **98 valid indexes**.
- `ix_legal_chunks_search_vector` is a valid GIN index. The valid cosine HNSW
  index `ix_lce_embedding_hnsw` uses `m=16` and `ef_construction=64`.
  Publication and chunk/source identity indexes are present.
- Representative `EXPLAIN ANALYZE` probes used the production publication join.
  A strict receipt/purchaser FTS query took **3.497 ms** server execution and
  a top-50 vector query took **9.695 ms**. On this release the planner used
  chunk-set and join indexes instead of GIN/HNSW for those probes. These are
  server timings. API latency includes remote database round trips, model
  work and context verification.
- Migration `20261111_19` adds default-deny RLS to the four optional Phase C
  tables created by `20261017_18`. Its SQL was generated offline. The
  shared database and its grants were left in place.

## Safe remediation and reproduction

The 18 v1 source-order defects are already superseded by the active v2 sets.
Historical records should be retained. For a verified active source problem,
preserve the PDF and the retrieval state backup, create a new extraction or
draft revision, review it, build an independent chunk set and its vectors,
validate a publication manifest, then activate that release explicitly. Keep
the prior manifest available for reviewed rollback. Inspect repeated sibling
labels and the numbering-glyph warning against the original PDF before
changing labels. A second-pass parser comparison cannot replace that review.

To repeat the read-only checks from the backend directory:

```powershell
.\.venv\Scripts\python -m scripts.audit_legal_database --output database-audit.json
.\.venv\Scripts\python -m scripts.audit_legal_chunks_readonly
.\.venv\Scripts\python -m scripts.audit_legal_embeddings --check-tokens --verify-model-sample 3
$env:LEGAL_UPLOAD_DIR = 'path-to-the-hash-addressed-source-pdfs'
.\.venv\Scripts\python -m scripts.audit_corpus_persistence_readonly
```

The persistence audit requires the hash-addressed originals. Keep audit exports
and database backups outside Git. Document content and vectors from a new
publication must stay within that publication's profile and revision scope.
