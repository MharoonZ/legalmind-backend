# LegalMind architecture review and implementation

Reviewed on **14 November 2026**, starting at commit 072fee2 on feature/ask.
This report supersedes the 11 November description of the answer pipeline.

## Implemented architecture

The backend uses FastAPI/Pydantic, SQLAlchemy async sessions, asyncpg,
Supabase PostgreSQL and pgvector. PyMuPDF and Tesseract support statutory
extraction. Docling supports a separate contract extraction workflow.
Sentence Transformers provides BGE-M3 embeddings and FlagEmbedding provides
the BGE cross-encoder. Generation supports Gemini and the existing OpenAI
adapter. The sibling frontend uses Next.js, React and Zod.

~~~mermaid
flowchart TB
  subgraph Offline["Corpus preparation · local administration"]
    Upload["Register PDF and law edition"] --> Hash["Validate bytes, hash and pages"]
    Hash --> Blob["Content-addressed original PDF"]
    Hash --> Job["Durable extraction job"]
    Job --> Extract["Page worker · native text and optional OCR"]
    Extract --> Pages["Pages, blocks, warnings and outcomes"]
    Pages --> Structure["Structure worker · provisions and exact spans"]
    Structure --> Review["Review coverage and approve revision"]
    Review --> Chunks["Build v2 chunks in physical source order"]
    Chunks --> FTS["English full-text index"]
    Chunks --> Vectors["Normalized BGE-M3 vectors"]
    FTS --> Publish["Validate manifest and atomically activate publication"]
    Vectors --> Publish
  end
  subgraph Online["Question answering"]
    UI["Next.js UI"] --> API["POST /ask"]
    API --> Query["Normalize original query and resolve law or section"]
    Publish --> Search["Scoped exact lookup or FTS and cosine search"]
    Query --> Search
    Search --> Fuse["Deduplicate and fuse ranks with RRF"]
    Fuse --> Rank["Score every candidate · preserve exact reference order"]
    Rank --> Expand["Complete roots, parents, qualifiers and references"]
    Expand --> Prompt["Bounded JSON prompt with source identities"]
    Prompt --> LLM["Configured provider · structured draft"]
    LLM --> Verify["Fresh publication and PDF span verification"]
    Verify --> Result["Verified quotations, citations and supported status"]
    Result --> UI
  end
~~~

Source identity is checked in repositories, context assembly, serialization
and citation verification. Retrieval and the cross-encoder order candidates.
Generation assesses legal support and missing facts from the supplied
sources. The EvidenceGate service, relevance heuristics and static fact-rule
injection have been removed from the runtime.

~~~mermaid
sequenceDiagram
  participant User
  participant API as FastAPI
  participant DB as PostgreSQL publication
  participant Models as Local models
  participant LLM as Answer provider
  User->>API: Question and explicit optional follow-up
  API->>DB: Active catalog and legal scope
  alt Numbered reference
    API->>DB: Exact root and child path lookup
  else Conceptual question
    API->>Models: Encode original normalized query
    API->>DB: Keyword and cosine candidates
    API->>Models: Score complete query/passage pairs
  end
  API->>DB: Expand complete source context
  Note over API,DB: Read-only transaction ends before provider call
  API->>LLM: Privileged instructions and evidence JSON
  LLM-->>API: Structured outcome and quoted claims
  API->>DB: Fresh quotation and publication verification
  opt Citation repair within configured limit
    API->>LLM: Verification code and unchanged evidence
    LLM-->>API: Revised draft
    API->>DB: Fresh span verification
  end
  API-->>User: Verified response with physical PDF pages
~~~

## Project structure and responsibility

| Location | Responsibility |
| --- | --- |
| app/main.py and app/api | Application lifecycle, HTTP transport, dependencies, status codes and local access restrictions |
| app/core | Validated environment settings, logging and exception types |
| app/schemas | API, extraction, source context and model draft contracts |
| app/services/qa | Answer orchestration, preparation, context, reranking, prompts, generation and citation proof |
| app/services/legal_*.py | Statutory extraction, parsing, chunking, query preparation and retrieval |
| app/services/contract_*.py | Private contract extraction and source attribution |
| app/repositories | SQL and transaction boundaries |
| app/db | ORM tables and shared connection pool |
| app/embeddings | Immutable profile, tokenizer, encoder and vector persistence |
| app/workers | Durable extraction and structure consumers |
| alembic/versions | Ordered schema history |
| scripts and tests | Operational entry points, audits, benchmarks and automated regressions |

Twelve answer modules moved into app/services/qa. Their imports in the API,
scripts and tests were migrated together. The existing transport contracts
are preserved. Parser and repository modules retain their established entry
points. The earlier inspection worker uses a separate repository interface
and remains supported by its callers and regression tests.

## Stage validation

| Stage | Input → output and implementation | Validation and failure behavior |
| --- | --- | --- |
| Upload | PDF/edition → hash-addressed original, file registration and job. legal_upload.store_legal_pdf and LegalIngestionRepository.register_upload | Streaming size bound, PDF/page validation, hash collision checks, metadata conflicts. Upload and ingestion API suites. |
| Page extraction | Claimed job/original hash → physical pages, raw and normalized blocks, geometry, warnings, outcome. workers/legal_extract and legal_extraction.extract_page | Native PyMuPDF or configured OCR. Blank candidates remain reviewable. Tests cover stale claims, retry, OCR and page failure. |
| Structure | Pinned extraction IDs → numbered provisions, hierarchy, schedules, notes and coverage. legal_structure.parse_structure and LegalStructureRepository | Parser version legal-structure-v12. Extensive regression suites cover source ownership, continuation, schedules and ambiguous glyphs. |
| Mapping | Provision fragments → exact half-open ranges in normalized PDF blocks. provision_spans, revision_sources and revision_block_coverage | Text equality, page identity and offset checks during audit, chunk validation and citation proof. |
| Review | Draft/coverage → explicit approved revision. legal_audit and local ingestion routes | Review events remain distinct from page extraction warnings. Approval does not establish current legal applicability. |
| Chunk | Approved revision/spans → independent v2 source-ordered chunks. legal_chunking.build_chunks_from_spans | 4,000-character soft limit at source-fragment boundaries. Exact offsets and sequence retained. Empty, missing or ambiguous source coverage is rejected. |
| Tokenize/embed | Full title/label/heading/body input → normalized 1,024-d vector. embeddings.encoder and EmbeddingRepository | Existing BGE-M3 profile, 8,192-token limit. Actual tokenizer checks before encoding. Hash, dimension and norm validation. No profile or source data migration. |
| Publish | Explicit chunk-set manifest → sealed and active publication. corpus_validation and PublicationRepository | Source reproduction, approval/vector audits and transactional activation. Locks prevent racing publication/writer changes. |
| Query | Original question/context → normalized query, references, scope and bounded alternative. legal_query | Original remains independent. Titles, years, identifiers and explicit follow-up scope are preserved. Combined search context now has a 4,000-character input bound. |
| Retrieve | Reference or vector/query → candidates from active approved publication. LegalSearchRepository and LegalRetrievalService | Parameterized SQL, exact child paths, FTS, cosine similarity and immutable profile scope. Publication change retries once. Missing law and technical failure have distinct responses. |
| Fuse | Ranked lexical/vector lists → RRF order. retrieval_fusion | Per-channel deduplication and sum of 1/(60+rank). Raw score magnitudes stay separate. |
| Rerank | Candidate list → scores for every complete pair, then configured top K. qa.reranker | Removed invented scores and hard-coded 256-token truncation. Exact lookups preserve all requested fragments. Tests cover long inputs, all scores, ordering, bad output and model failure. |
| Context | Ranked roots → complete siblings, parents, qualifiers and verified references. qa.context and QaContextRepository | Required groups are atomic. Optional incomplete definitions roll back. A conceptual request may retry its top complete root with partial coverage disclosed. |
| Source contract | Prepared units → consistent publication/version, source refs, selected text and budget. qa.context_validation | Integrity checks only. Low logits and different wording never filter candidates here. Tampered IDs, text, offsets and mixed editions fail explicitly. |
| Prompt | Original/resolved question and source context → bounded privileged instruction plus JSON. qa.prompt | Prompt injection boundaries restored. Missing references and limited coverage require partial/insufficient outcomes. Complete serialized prompt capped at 128 KiB. |
| Generate | Packet → structured draft. qa.generation | Gemini systemInstruction separated from data. Configured output token limit restored. Deadline, connect-only retry, completed response check, schema validation and safe error logging. |
| Citations | Draft claims → exact source-verified quotes and server metadata. qa.citations and qa.source_verification | Fresh DB snapshot after generation. All quoted characters need coverage. Requested subsection paths must be cited. One bounded citation repair by default. |
| Response | Verified claims/status → AskOut. qa.answer and API route | Public legal text is assembled from verified quotations. Model-supplied prose cannot bypass claim verification. Missing evidence, ambiguity, provider failure and invalid input keep distinct outcomes. |
| Contract workflow | Private PDF → document/version, then pages/clauses/spans. ContractExtractionService | Separate from statutory retrieval. Hashing and Docling now run outside the API event loop. Existing loopback restrictions remain required while ownership/authentication is absent. |

~~~mermaid
erDiagram
  legal_instruments ||--o{ legal_versions : versions
  legal_files ||--o{ file_extractions : extracted
  file_extractions ||--o{ file_pages : pages
  file_pages ||--o{ file_blocks : blocks
  legal_versions ||--o{ corpus_revisions : revises
  corpus_revisions ||--o{ revision_sources : pins
  corpus_revisions ||--o{ provisions : structures
  provisions ||--o{ provision_spans : maps
  file_blocks ||--o{ provision_spans : supports
  corpus_revisions ||--o{ chunk_sets : chunks
  chunk_sets ||--o{ legal_chunks : contains
  legal_chunks ||--o{ legal_chunk_provisions : maps
  legal_chunks ||--o{ legal_chunk_embeddings : embeds
  embedding_profiles ||--o{ legal_chunk_embeddings : defines
  corpus_publications ||--o{ publication_items : selects
  chunk_sets ||--o{ publication_items : includes
  qa_runs ||--o{ retrieval_hits : records
  qa_runs ||--o{ answer_claims : contains
  answer_claims ||--o{ answer_evidence : cites
~~~

## Prioritized findings and implemented corrections

| Priority | Starting defect and impact | Correction |
| --- | --- | --- |
| High | Free-form answer was returned even though only quoted claims were verified. Unrelated prose could carry unsupported conclusions | Assemble public legal text from verified quotes. Existing claim/citation response fields remain usable |
| High | Context source-identity and completeness checks had been removed from the old screening service | Restore checks as a source-data contract at prompt construction, independent of relevance |
| High | Reranker scored four candidates and synthesized remaining logits | Score every candidate with the configured model, retain actual scores and exact lookup ordering |
| High | Silent 256-token truncation could hide a condition or exception at the end of a passage | Actual tokenizer check and complete configured pair budget |
| High | Six-passage context limit rejected a valid subsection whose required qualifications needed nine short passages | Increase the count limit to twelve, retain existing token/character limits and add a complete-group regression test |
| High | Prompt lost instruction/data isolation, partial coverage and missing-fact behavior | Evidence-only system instruction, JSON packet and explicit structured outcome requirements |
| Medium | Gemini output token setting was ignored and provider bodies could enter logs | Apply configured budget and redact provider response/exception contents |
| Medium | “This Act” was captured as an unknown explicit title | Preserve contextual law resolution |
| Medium | A valid selected instrument failed to resolve a shared title | Use the verified matching instrument to disambiguate |
| Medium | Search follow-up could exceed supported combined query size | Validate total length before retrieval |
| Medium | Synchronous contract hashing/Docling blocked the asynchronous caller | Offload CPU/file extraction work to a worker thread |
| Medium | Optional audit-recording failure replaced a verified answer with a service error | Preserve the verified response and log the recording failure without private details |
| Medium | Chunk audit buffered bulk reads and repeated an unused set report per chunk | Stream bounded batches and fetch each set report once |
| Maintenance | Answer modules spread across services root and stale relevance/fact-rule dependencies remained | Group under app/services/qa, remove obsolete service and injection path, update callers and tests |

## Configuration retained

The BGE-M3 embedding profile and BGE reranker model are preserved.
Search uses pool 50, cosine minimum 0.48, RRF k 60 and HNSW ef_search 200.
Reranker top K defaults to five, with a full pair budget of 8,192 tokens.
The passage-count default increased from six to twelve after a live
subsection lookup required nine passages totaling 971 tokens and 3,938
characters. The 16,000-character limit, 3,000-token limit and reference depth
one remain in effect. A regression test preserves a qualification-heavy root
as a complete group.

Gemini's current configured default remains gemini-3.5-flash-lite with a
90-second provider timeout and 1,200 output tokens. Overall ask timeout is
180 seconds. Provider selection remains configurable. The sample environment
now agrees with those defaults. The optional QA recorder remains off by default.

## Deployment and limitations

Install the existing requirements, supply the backend environment, cache
the selected model weights, review/apply Alembic on the intended database
and validate a publication manifest. Run the FastAPI process and the
extraction/structure workers separately. Run hosted services behind explicit
allowed origins, workload limits and a restricted server database role.
Keep credentials, private source files and DB exports outside Git.

The configured database has an active pilot of 23 instruments and 531
embedded chunks. Contract Act 1872 is absent. All 23 editions have unknown
applicability metadata. Software changes cannot certify legal applicability
or add missing approved law.

Exact source checking proves quotation provenance. Contextual interpretation,
cherry-picking and legal completeness still need realistic provider evaluation
and legal review. This change intentionally returns source-verified quotations
because the starting prose path lacked equivalent support verification.
Live Gemini continuation results are recorded below. CPU inference and
remote DB round trips still limit throughput and tail latency.

## Reproduction

Run these commands from the backend directory.

~~~powershell
.\.venv\Scripts\python -m pytest -q -rs
.\.venv\Scripts\python -m scripts.audit_legal_database --output audit.json
.\.venv\Scripts\python -m scripts.audit_legal_chunks_readonly
.\.venv\Scripts\python -m scripts.verify_qa_pipeline_readonly --output api-check.json
.\.venv\Scripts\python -m scripts.benchmark_legal_rag --output rag-check.json
~~~

The API verifier uses the real database and source-proof path with an
in-memory Gemini HTTP response. The benchmark defaults to preparation-only
mode. Use its explicit Gemini option for actual generation after configuring
the credential. Prepared source context alone cannot establish legal
relevance or correctness.

## Executed validation on 14 November

The starting branch completed with **680 passed, 98 failed and one skipped**.
The final revised full suite completed with **755 passed and one skipped** in
48.64 seconds, including the regression added after live provider testing.
The skipped worker-concurrency test needs a disposable
loopback PostgreSQL database. The old relevance-screening and static
fact-injection tests were retired with those features. Their source identity
cases were retained under test_qa_context_validation, and additional tests
cover preparation without a relevance classifier, missing context, explicit
instrument selection, input bounds, provider privacy and recorder failure.
The counts therefore cover different supported feature sets.

All **23 frontend browser tests passed**. Actual API execution with Supabase
and an offline Gemini transport passed complete, partial, missing-reference
and ambiguous-reference outcomes, plus invalid-input validation. Supported
claims were independently attributed to physical page 7 of 477.pdf.
The partial case preserved section 19 support and disclosed missing section
999. The offline transport cannot measure Gemini answer quality.

The fresh read-only database audit observed migration 20261111_19,
23 instruments, 87 revisions, 12,268 historical provisions, 46 chunk sets,
1,060 historical chunks and 531 active-profile embeddings. The four QA audit
tables exist and are empty. There were zero completed-extraction page gaps,
cross-revision parent mismatches, exact span text/identity mismatches,
chunk-link identity mismatches or chunk-sequence errors. The 82 repeated
sibling-label groups across revision history and unknown applicability for
all 23 active editions remain review findings.

The first broad chunk audit timed out while buffering records. Both streamed
retries completed. Across all 1,060 chunks the final audit found zero exact
source-span mismatches, zero empty v2 chunks, zero v2 search-vector issues,
zero excluded source links and zero duplicate v2 text groups. The 18
historical v1 parent-continuation ordering defects remain outside the active
v2 publication.

### Focused real retrieval run

This run used the actual API, configured Supabase, BGE embeddings and
cross-encoder. Generation was deliberately disabled. The four supported
questions retrieved all five labeled root provisions within the top five,
with Recall@5/10 of 1.000 and MRR@10 of 1.000. The sample is small and
provides a regression check. A fresh before/after retrieval quality
comparison was not established for this review.

| Question category | Search result | Preparation |
| --- | --- | --- |
| Receipt exact | Consumer Protection section 19 at rank 1 | Complete source context |
| Receipt paraphrase | Consumer Protection section 19 at rank 1 | Complete source context |
| Receipt spelling errors | Consumer Protection section 19 at rank 1 | Reached overall request deadline |
| Multiple sections | Prisons Act sections 31 and 32 at ranks 1 and 2 | Complete source context |
| Invented section | No matching reference | Insufficient evidence |
| Missing follow-up context | Ambiguous input | Clarification |

Search P50/P95 was **14.287/47.790 seconds**. Ask P50/P95 was
**53.267/182.011 seconds**, excluding real provider generation. Ready
preparation responses returned the benchmark's deliberate provider-unavailable
503. The spelling case timed out before preparation finished. This failure
remains visible and prevents a production latency claim.

The durable verification summary is in
[review_20261114.json](verification/review_20261114.json). Full local logs and
responses are under the workspace tmp directory with the review-20261114
prefix. The preparation-only runs above preceded live Gemini validation.
The continuation below records real provider outcomes. Representative load
testing and broader reviewed answer evaluation remain deployment requirements.


### Isolated deadline retest

The spelling case was repeated with the other checks finished. It retrieved
section 19 at rank one and prepared the retained source group in **149.054
seconds**. Coverage was limited, so generation can provide partial support
or abstain. The earlier deadline failure did not reproduce in this rerun.
Latency and variation remain deployment limitations.

### Live Gemini continuation

The supplied local settings were normalized into the ignored backend .env.
Gemini model access succeeded, and the actual API then ran against the
configured Supabase publication, cached BGE models and
gemini-3.5-flash-lite. No provider responses were mocked in these runs.
The legacy MiniLM environment fields do not change the active publication's
pinned BGE-M3 profile. Approval scope is enforced by the publication queries.

The first eight-case run exposed a real context-budget defect: the requested
section 4(2)(a) had a nine-passage required source group, while the passage
limit allowed only six. The complete group contained just 971 tokens and
3,938 characters. Raising only the default passage-count limit to twelve
resolved the rejection. The token, character and reference-depth limits
remain unchanged. The new regression preserves a root with seven required
qualifications and its parent as a complete group.

The corrected subsection lookup and two additional questions were then run
with the actual provider. The table combines the eight original cases with
the three-case continuation, replacing the initial subsection failure with
its corrected retry. The original failure remains in the local raw result.

| Case | Final API outcome | Citations | Ask seconds |
| --- | --- | ---: | ---: |
| Receipt exact reference | Answered | 5 | 64.710 |
| Receipt paraphrase | Answered | 5 | 66.373 |
| Section 4(2)(a) | Answered after correction | 1 | 62.689 |
| Prisons Act sections 31 and 32 | Answered | 2 | 35.489 |
| Receipt section 19 plus unavailable section 999 | Partial | 5 | 36.195 |
| Receipt follow-up with previous question | Partial | 5 | 171.576 |
| Instruction to invent a section | Insufficient evidence | 0 | 9.984 |
| Unpublished Contract Act | Insufficient evidence | 0 | 4.554 |
| Unrelated science question | Insufficient evidence | 0 | 12.767 |
| Follow-up without context | Clarification needed | 0 | 4.508 |

All ten distinct cases returned HTTP 200 with the supported, partial,
abstaining or clarification outcome appropriate to this focused check.
The six supported/partial cases contained seven quoted claims and 23
citations. The real API verified the quoted source spans. Additional public
response checks confirmed claim/citation links, publication identities,
requested subsection paths and expected PDF pages. Receipt citations point
to physical page 7 of 477.pdf, the subsection to page 5, and Prisons Act
sections 31 and 32 to pages 10 and 11 of 13.pdf.

The partial section request explicitly disclosed unavailable section 999.
The follow-up disclosed limited verified context and returned the receipt
duty, without asserting a remedy for refusal. These checks establish a
small regression result, not independently reviewed legal accuracy.

Across these ten final outcomes, end-to-end ask P50 was **35.842 seconds**,
linearly interpolated P95 was **124.235 seconds**, and the maximum was
**171.576 seconds**. Provider-only timing and cost were not measured
separately. The follow-up remains close to the overall request deadline.
Representative legal evaluation, throughput testing and latency work remain
necessary before a production readiness claim.

The sanitized per-case results and context-budget correction are recorded in
[review_20261114_live.json](verification/review_20261114_live.json).
The final full backend suite passed with **755 passed, one skipped and ten
dependency deprecation warnings**. The previously completed 23 frontend
browser tests also passed. No production corpus/schema changes were needed
for the live validation.
