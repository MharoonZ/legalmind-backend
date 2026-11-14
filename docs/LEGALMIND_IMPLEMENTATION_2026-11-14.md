# Implementation continuation: conversational answers and bounded inference

## Audit before changes

The preceding system audit and live baseline are in LEGALMIND_REVIEW_2026-11-14.md.
This continuation starts at 4fa5c51 on feature/ask.

Confirmed causes in the current implementation:

1. The prompt requires claim_text to equal a quotation, and answer.py discards
   generated explanations. Scenario analysis therefore cannot reach the user.
2. Reranking runs all pairs in one uninterruptible call. Cancelling the HTTP
   coroutine cannot stop its running thread, including work waiting on model locks.
3. Native inference uses the default executor without admission bounds. Embedding
   and reranking have separate locks and can compete for CPU resources.
4. Reranker batches are padded together without reporting token lengths, padding
   costs or inference time. CPU thread counts are not configured by the app.
5. Context assembly retries just the first root after any multi-root failure,
   unnecessarily losing other complete, relevant roots that could fit.
6. Context timing currently includes reranking time. Query preprocessing,
   catalog loading and fusion lack distinct timing measurements.
7. Gemini creates and closes a new HTTP client per generation/repair call.
8. The API receives one earlier question, with no bounded multi-turn history.
9. Pure greetings enter the legal retrieval pipeline.
10. At the start, the uploads directory contained only the legacy database backup and
    OCR language data: zero PDFs were found recursively. Stored PDF object paths
    had to be reconciled before original-page verification or reprocessing.
    This blocker was subsequently resolved using the user's Downloads folder.

Implementation preserves publication scope, exact quotations, pinned BGE-M3,
the reranker model, original IDs and source data. No destructive migrations or
EvidenceGate/EvidenceGateway components are planned. Natural explanations will
be linked to verified quoted claims and checked separately by the provider.
That semantic review remains model-based, with explicit limitations.

## Work order

Bound native inference and add cancellation checkpoints; measure CPU batching;
retain complete context groups; enable cited scenario explanations; add bounded
conversation input and a cheap greeting route; reconcile local PDF inventory;
update diagrams and record focused real runs. Test changed behavior in focused
suites and run the full backend suite once at the end.

## Changes implemented

### Performance and request lifecycle

The pinned BGE model and reranker remain unchanged. The reranker's classifier
now receives the already checked paired token IDs directly, eliminating the
library's trial forward pass on the largest batch and duplicate tokenization.
Every candidate receives a real score. Inputs are never shortened. Length-sorted
batches obey both the configured batch size and a 2,048 padded-token budget,
with a complete oversized pair allowed as a singleton. Original ordering is
restored after scoring. The runner uses inference_mode and the existing device.

Native embedding/reranking work shares a single process executor with capacity
for one running and one waiting operation. Excess work fails explicitly. Queued
cancelled jobs never start; running reranker work observes cancellation between
batches. Capacity stays occupied until native work actually finishes. A current
forward pass cannot be forcibly interrupted by Python cancellation. Four /ask
requests may be admitted per process by default; excess requests retain the
existing structured 503 response contract. These are process-level bounds, not
a distributed rate limiter. The overall deadline stays at 180 seconds.

Gemini connections are reused across generation, review and repair and closed
on application shutdown. Prompt source spans, hashes and coordinates remain
verified server-side; repeating those machine identifiers per fragment in the
provider prompt was unnecessary token overhead and has been removed.
Source-proof queries now project the fields actually used for proof construction,
instead of transferring full page text and extraction reports again per span.
The source identity, complete character-range and publication checks are retained.

Logs distinguish conversation resolution, query preprocessing, catalog reads,
embedding queue/inference, keyword/vector reads, fusion, reranking, context,
generation, exact citation verification, semantic explanation review and total
request time. Provider token usage remains logged without prompt contents.

### Complete context and useful explanations

Conceptual retrieval now packs ranked source groups inside one DB snapshot.
A root commits only with its whole text, parents, qualifications and required
references. A failed group rolls back atomically and leaves room for other
complete roots. This replaces the previous all-or-first-root retry. Budgets are
checked before loading spans for groups that cannot fit. Exact numbered lookups
retain their stricter all-requested-reference behavior.

Omitting a lower-ranked candidate does not itself prove that an answer is
incomplete. The generator must assess the relevance and materiality of omissions;
missing explicitly requested references still require partial support or refusal.
No retained rule may lose required qualifications to make room for explanation.

Quoted claims still undergo exact character, publication, edition and PDF span
verification. A draft may also contain explanation/application paragraphs linked
to those verified claims. Applications identify exact user fact quotations.
The server rejects nonexistent claim indexes and invented user facts. A separate
structured provider review then checks the explanation against its cited claims,
full retained source context, conditions, logical conjunctions and user statements.
Rejected explanations receive bounded repair feedback through the existing repair
loop. Unverified free-form draft.answer is still never displayed. Accepted
paragraphs receive server-generated citation numbers in the response.

Semantic review is model-based and can miss an interpretation error. It does not
certify legal correctness, factual truth, current law or applicability. Exact
quote provenance remains deterministic. No EvidenceGate/EvidenceGateway service,
legal rule classifier or static legal answer was introduced.

### Conversation and integration

The optional history input accepts up to six user/assistant turns, each at most
4,000 characters and 12,000 characters in total. Existing context.previous_query
clients remain supported. A small provider request selects relevant existing user
turn indexes for a follow-up, or none for a new topic. It cannot write facts or
select assistant messages as facts. Selected user text is copied exactly into
the existing retrieval context; the combined question budget remains 4,000.
Pure greetings and thanks use a cheap whole-message route without retrieval or
generation. A greeting mixed with a legal question still goes through RAG.

The frontend carries bounded history between messages and clears it on New
question. Two-character greetings are accepted. The response fields and status
values are unchanged. Quoted claims and PDF citations remain available for
inspection alongside natural explanations.

Conceptual questions naming multiple unambiguous published laws can now search
that explicit set of instruments. Mixed numbered references whose law binding
is ambiguous still request clarification. SQL scope remains parameterized and
restricted to the active approved publication and pinned embedding profile.

## PDF processing and data integrity

The initial uploads inventory found zero local PDFs. The user then identified
C:\Users\Admin\Downloads\uploads, containing 46 copies of 23 unique originals.
All 23 matched the registered hashes and metadata and were restored to the
application's existing content-addressed paths. No document records, chunks,
embeddings or approvals were created or changed. Their filenames, hashes,
page counts, law titles and current matched status are recorded in
[the PDF inventory](verification/implementation_20261114_pdfs.json).

The audit_uploaded_pdfs command reconciles nested original files
by content hash, inspects native-text presence and page counts when files exist,
and reports missing originals or metadata mismatches. Its explicit
--restore-missing option copies only verified missing bytes atomically and
rejects existing conflicting blobs. It never creates duplicate documents,
chunks or embeddings, changes IDs, approves revisions or edits the supplied PDFs.
No corpus or schema mutations were needed for this implementation.

Stored source text and the existing active publication were used for real
retrieval, generation and citation verification. Byte verification and fresh
extraction comparison now cover the whole active collection; six original
pages also received visual spot checks:
199 raw pages and 1,548 raw blocks matched, 2,473 chunk/source links had no
mismatches, and all 531 embeddings passed identity/input checks. Nineteen
distinct prior citation fragments matched their actual original pages.
The one normalized glyph difference exposed a quotation-integrity gap, now
protected by comparing quoted characters with the preserved raw source text.
See the [original PDF validation report](LEGALMIND_ORIGINAL_PDF_VALIDATION_2026-11-14.md).

## Implemented workflow diagrams

### 1. System architecture

~~~mermaid
flowchart LR
  Admin[Local administration] --> Ingest[PDF ingestion and review]
  Ingest --> DB[(PostgreSQL and pgvector)]
  UI[Next.js conversation UI] --> API[FastAPI /ask]
  API --> Prepare[Retrieval and complete context]
  Prepare <--> DB
  Prepare --> Runtime[Bounded local model runtime]
  Prepare --> Generate[Structured provider generation]
  Generate --> Proof[Exact source verification]
  Proof <--> DB
  Proof --> Review[Provider review of cited explanation]
  Review --> UI
~~~

### 2. Upload and processing

~~~mermaid
flowchart LR
  PDF[Original PDF bytes] --> Validate[Validate hash, size and physical pages]
  Validate --> Store[Content-addressed file and existing identity]
  Store --> Job[Idempotent extraction job]
  Job --> Worker[Extraction worker]
  Worker --> Structure[Structure worker]
  Structure --> Human[Review coverage and approve revision]
  Human --> Publish[Build and validate publication]
~~~

### 3. Extraction and chunking

~~~mermaid
flowchart LR
  Pages[PDF physical pages] --> Extract[Native text or necessary OCR]
  Extract --> Blocks[Ordered blocks and page outcomes]
  Blocks --> Parse[Provision hierarchy and source spans]
  Parse --> Approved[Approved source revision]
  Approved --> Chunks[Source-ordered legal chunks]
  Chunks --> Check[Verify complete coverage, offsets and sequence]
~~~

### 4. Embedding and storage

~~~mermaid
flowchart LR
  Chunk[Approved chunk with law metadata] --> Recipe[Pinned input recipe and hash]
  Recipe --> Tokens[Complete tokenizer length check]
  Tokens --> BGE[BGE-M3 normalized 1024-dimensional vector]
  BGE --> Store[(Profile-scoped vector persistence)]
  Store --> Manifest[Validate vectors and source manifest]
  Manifest --> Active[Atomic active publication]
~~~

### 5. Retrieval and reranking

~~~mermaid
flowchart TB
  Query[Original question and selected user context] --> Scope[Resolve law and numbered references]
  Scope --> Exact[Exact provision lookup]
  Scope --> Hybrid[FTS and pinned-profile vector search]
  Hybrid --> RRF[Deduplicate and fuse ranks]
  RRF --> Runner[Bounded native runner]
  Runner --> Tokens[Full paired tokens and length-sorted batches]
  Tokens --> Scores[One classifier pass per batch]
  Scores --> Rank[Restore scores and select ranked candidates]
  Exact --> Context[Complete context groups]
  Rank --> Context
~~~

### 6. Answer generation

~~~mermaid
flowchart LR
  Groups[Complete retained source groups] --> Prompt[Bounded evidence JSON and instructions]
  Prompt --> Draft[Quoted claims and explanation paragraphs]
  Draft --> Quotes[Exact quotation and source verification]
  Quotes --> Analysis[Fact and claim links plus semantic review]
  Analysis --> Answer[Natural explanation and server citation numbers]
~~~

### 7. Citation and explanation verification

~~~mermaid
flowchart TB
  Draft[Untrusted draft] --> IDs[Check allowed evidence identities]
  IDs --> Quote[Locate complete exact quotations]
  Quote --> Snapshot[Fresh publication and source-span snapshot]
  Snapshot --> Raw[Compare quoted characters with preserved raw source]
  Raw --> Cite[Server-generated PDF metadata]
  Cite --> Facts[Check analysis claim links and exact user fact quotations]
  Facts --> Review[Review support and material qualifications]
  Review -->|Supported| Public[Compose public answer]
  Review -->|Rejected with remaining attempt| Repair[Bounded repair using original evidence]
  Repair --> Draft
  Review -->|No supported draft remains| Abstain[Honest insufficient evidence response]
~~~

### 8. Complete request lifecycle

~~~mermaid
flowchart TB
  Message[User message] --> Capacity[Request capacity bound]
  Capacity --> Greeting{Pure greeting or thanks?}
  Greeting -->|Yes| SmallTalk[Conversational response]
  Greeting -->|No| History[Select existing relevant user turns]
  History --> Search[Resolve query and retrieve approved sources]
  Search --> Rank[Rerank through bounded native runtime]
  Rank --> Context[Pack whole source groups and dependencies]
  Context --> LLM[Generate structured cited explanation]
  LLM --> Verify[Fresh exact source proof and explanation review]
  Verify --> Response[Compatible JSON answer, claims and citations]
  SmallTalk --> Response
  Capacity -->|Full| Busy[Structured service unavailable]
~~~

## Running and validation

Existing backend/frontend startup instructions remain valid. The server uses
cached BGE model weights and the ignored .env configuration. Optional tuning:

~~~dotenv
QA_MAX_CONCURRENT_REQUESTS=4
QA_RERANK_BATCH_TOKEN_BUDGET=2048
~~~

These values change admission/padding bounds, not the model, embedding profile,
pair-token limit or request deadline. Additional API processes each load their
own models and must be sized against available RAM and CPU.

~~~powershell
.\.venv\Scripts\python -m scripts.audit_uploaded_pdfs --output pdf-inventory.json
.\.venv\Scripts\python -m scripts.benchmark_legal_rag --gemini --all-answers --only appliance-fire-scenario
.\.venv\Scripts\python -m pytest -q -rs
~~~

The first real appliance scenario run measured the original reranker at
**98.673 seconds** on ten candidate pairs. Removing the duplicate forward pass
and tokenization took **51.919 seconds on the identical pairs, with zero score
difference** in that run. This is a measured component improvement of 47.4%.
The CPU is a four-core Intel i5-10310U with no CUDA device. No model migration,
quantization, hidden truncation or invented scores was used.

That initial revised real API run returned the appliance scenario as answered
in **109.851 seconds**, with two explanatory paragraphs and ten verified source
citations. The follow-up about kitchen repair costs returned answered in
**129.049 seconds**, after rejecting and repairing one unsupported explanation.
A pure greeting returned in **0.091 seconds** without retrieval. An invented
section returned insufficient evidence with no citations. These measurements
precede the final token-budget batching, compact prompt and budget-prefetch
changes; final measurements are recorded separately below when available.

### Final measurements and verification

With token-budget batching, reranking the same ten pairs took **35.979 seconds**,
a **63.54% reduction** from the 98.673-second baseline. The maximum score
difference was **0.000003815**, consistent with floating-point batch variation.
No candidate or source text was dropped from scoring.

The final real scenario response contained four cited explanation/application
paragraphs, three quoted claims and nine source citations. It returned answered
in **164.702 seconds**. This slower total must not be represented as an overall
speed improvement: retrieval took 50.564 seconds, context assembly 50.619 seconds,
and citation reads 21.060 seconds in that run. Reranking took 35.989 seconds,
generation 4.916 seconds and semantic review 1.542 seconds. The local CPU and
remote DB measurements varied substantially between runs.

The exact subsection lookup returned answered with its page-5 citation in
**67.994 seconds**, without using the reranker. Its explanation was also reviewed.
The final prompt used **5,472 provider input tokens** for this case. The compact
scenario prompt used fewer machine identifiers than the initial 9,683-token run;
per-call token measurements remain in the local log rather than a fabricated
fixed saving across different generated drafts.

After those API runs, source-proof payload projections were narrowed. A separate
real DB check verified **four claims across ten source spans** through the
modified chunk/provision proof queries in **22.380 seconds**, including setup
and input reads. No fresh whole-API performance claim is made after this final
query projection change. Real Gemini conversation selection separately passed
both a kitchen-damage follow-up and an unrelated new prison-law topic.

The full backend run reported 759 passed, five fixture failures and one skipped.
The failures were a misplaced variable in the new packing test and four SDK
fixtures missing the new analysis field. These were corrected. The affected
suites then passed **134 tests**, and a final source-query/context recheck passed
**103 tests**. Across the full run and focused rechecks the final covered status
is **764 passed, zero failed and one skipped**; a second full suite was not run.
Frontend type checking and lint passed. The two focused browser checks passed
request submission, citation inspection and legacy factual follow-up behavior.

Durable results:

- [Implementation measurements and cases](verification/implementation_20261114.json)
- [Original PDF inventory](verification/implementation_20261114_pdfs.json)
- [Real conversation context checks](verification/implementation_20261114_conversation.json)
- [Projected source-proof verification](verification/implementation_20261114_projected_proofs.json)

Four appliance/manufacturer question formulations were added to the existing
regression dataset. Only the recorded real cases above were executed in this
continuation; adding a case is not evidence that every formulation passed.

## Remaining limits

The original-file blocker is resolved. All 23 editions still have unknown legal
applicability. Semantic support review is probabilistic and needs expert review
over a broader legal evaluation set. CPU inference and remote DB latency still
limit response time and concurrent throughput. Public deployment still needs
authentication, document ownership, distributed rate controls and operational
deployment work; existing administration remains restricted to local development.
The worker locking integration test needs a disposable local PostgreSQL DB.
