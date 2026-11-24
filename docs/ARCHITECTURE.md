# LegalMind architecture

This document describes the system as it is now: what each stage does, where
its code lives, and why it works the way it does. The history of how it got
here is in [AUDIT_2026-11.md](AUDIT_2026-11.md).

## Overview

```
                 Phase A: ingestion                      Phase B: indexing
PDF --upload--> pages --extract/OCR--> legal structure --review--> approved revision
                                                                        |
                              chunks --> keyword index --> embeddings --+--> publication (immutable)
                                                                                   |
                 Phase C: question answering                                       v
question --> understand --> retrieve --> rerank --> evidence --> generate --> verify citations --> answer
                              ^
                              +---- in-memory snapshot of the active publication
```

Three rules hold throughout:

1. **Only published text is served.** A publication is an explicit, immutable
   list of approved chunk sets with a manifest hash. Nothing reaches users until
   a publication is activated, and activation never picks "the latest" by itself.
2. **Every quotation is traceable.** Each chunk keeps the mapping from its
   characters to the provision and the PDF page they came from, so a citation
   can name the law, the section and the page.
3. **A question is never refused for how it is written.** Spelling mistakes,
   abbreviations, missing words and follow-ups narrow or widen the search; they
   do not turn into errors.

## Phase A: ingestion (`app/services`, `app/repositories`, `app/workers`)

| Stage | Code | Notes |
|---|---|---|
| Upload | `api/routes/legal_ingestion.py`, `services/legal_upload.py`, `repositories/legal_ingestion_repository.py` | The PDF is validated and stored under its SHA-256. Re-uploading the same bytes reuses the file and its job. |
| Page extraction | `workers/legal_extract.py`, `services/legal_extraction.py`, `services/legal_tables.py` | One outcome per physical page: native text, OCR, blank or needs review. A page whose table detection failed is sent to review. |
| Legal structure | `workers/legal_structure.py`, `services/legal_structure.py`, `services/legal_status.py`, `services/legal_relationships.py` | Parses parts, chapters, sections, sub-sections, schedules and amendment notes into a provision tree with source spans. |
| Review and approval | `services/legal_audit.py`, `services/corpus_validation.py`, `api/routes/legal_inspection.py`, `services/legal_preview.py` | A revision is approved explicitly; audit findings stay attached to it. |

The two workers claim jobs with a lease, renew it page by page, release the job
when they are stopped, and retry transient database errors with backoff
(`workers/runner.py`). Several workers can run at once.

These routes are local administration tools. They are mounted only when
`ENVIRONMENT=development` and `LEGAL_INGEST_ENABLED=true`, and the request
middleware rejects any caller that is not on the same machine before the
request body is read.

## Phase B: indexing (`app/services/legal_chunking.py`, `app/embeddings`, `scripts/pipeline`)

| Stage | Code | Notes |
|---|---|---|
| Chunks | `services/legal_chunking.py`, `repositories/legal_chunk_repository.py` | One chunk per provision where possible; long provisions are split on sub-section boundaries. Each chunk records which characters came from which provision and page. |
| Keyword index | `legal_chunks.search_vector` (PostgreSQL) | Kept for the audit scripts. The API searches its in-memory index instead. |
| Embeddings | `embeddings/encoder.py`, `embeddings/profiles.py`, `embeddings/repository.py` | BAAI/bge-m3, 1024 dimensions, stored with pgvector. The profile name is part of every vector's identity. |
| Publication | `repositories/publication_repository.py`, `db/publication_models.py`, `api/routes/publications.py` | A manifest of chunk sets is validated (hashes, vector coverage, one edition per law) and stored; activation switches the single active publication. |

## Phase C: question answering (`app/retrieval`, `app/qa`)

### The publication snapshot (`retrieval/snapshot.py`, `loader.py`, `index.py`)

Two database queries check the active publication and its pinned sources. Each
revision must still be approved, the revision and chunk-set hashes must agree
with the manifest, and both chunk counts and embedding coverage must be complete.
On a cold load, three further queries stream the chunks with binary pgvector
embeddings, provision nodes and page mappings in batches. A worker thread
builds the immutable snapshot, including the BM25 index, law resolver and
cross-references. It also checks vector dimensions, norms and chunk ownership.

The disk cache is keyed by publication ID and manifest hash. A cache hit still
requires both database checks. The metadata records a checksum for the vectors.
Corrupt pairs are discarded and rebuilt. Cache writes sync both temporary
files before publishing the metadata and keep the current and previous
generations. Cache I/O runs on a worker thread.

Every `PUBLICATION_REFRESH_SECONDS` (60 by default), a background task checks
approval and completeness again. A transient database failure preserves the
loaded snapshot. Invalid publication data clears it. When no snapshot is
available, concurrent requests share a load attempt and wait one refresh
interval before retrying. Once loaded, question retrieval runs entirely in memory.

The design fits collections up to the low hundreds of thousands of chunks
(vectors take 4 KB per chunk, so 100,000 chunks need about 400 MB). Beyond
that the vector search should move to pgvector's index and the snapshot
should keep only text and structure.

### 1. Understanding the question (`retrieval/query.py`, `laws.py`, `references.py`, `text.py`)

- **Provision references.** `parse_references` reads "section 3", "sec. 3",
  "s 3", "u/s 3", "sction 3", "section three", "sections 3 and 4", "sections 3
  to 5", "sections 4 to 8, 11 and 18 to 22", "section 5(2)(a)", "sub-section
  (2) of section 5" and schedules in any numbering style. A number followed by
  a unit ("3 years") is not a reference.
- **Law names.** `LawResolver` matches the words of the question against the
  words of every published title, weighting rare words more than common ones.
  A word matches exactly, by stem ("Prison" and "Prisons"), or within a small
  edit distance that grows with word length ("Punjb", "Gamblng", "Ordinence").
  Years and type words (Act, Ordinance) adjust the score. The outcome is one
  law, several candidates (the user is asked which), or "a law was named that
  is not in the collection". An unknown law is never silently replaced by a
  similar one. Distinctive qualifiers in an explicit title must match. An
  explicit instrument ID outside the publication produces an unresolved law.
- **Follow-ups.** With conversation history, a message such as "and section
  5?", "what is the penalty for that?" or "the second one" inherits the law,
  and when needed the previous question's wording, from earlier turns. A law
  named in the current message restricts the search. A remembered law provides
  a small ranking tie-breaker. A follow-up that supplies a new subject, even
  one word such as “receipts”, searches for that subject. A fresh named
  question starts a new lookup.
- **Bare references.** "section 3" with no law looks at which laws have a
  section 3. The topic is scored against the requested provision in each
  candidate law. If several laws have the provision and the topic yields no
  clear winner, the service asks which law the user means.

All of this is deterministic and uses no model call. Nothing here is specific
to a law, a section number or a question.

### 2. Retrieval (`retrieval/service.py`, `lexical.py`)

- **Exact lookup** returns matching passages in the order asked, including
  ranges, up to the caller's limit. The response identifies missing provisions
  even when the passage list is truncated. “The Schedule” includes all numbered
  schedules in order. “Schedule 1” also matches a law's sole unnumbered schedule.
- **Hybrid search** otherwise: BM25 over title, citation, heading and text,
  plus cosine similarity of the question's embedding against every chunk
  vector, merged with Reciprocal Rank Fusion. At most eight unknown words get
  a vocabulary lookup per query. Short words can be repaired for a missing
  letter, extra letter or adjacent transposition.
- **Degradation.** If the embedding model is unavailable the search continues
  with keywords alone and says so. If a named law has nothing on the topic,
  the service searches the collection using the subject of the question with
  the title removed. A stronger match from another law is identified in the
  retrieval warnings.

Query embeddings are cached and computed on a single inference thread with a
bounded queue (`core/inference.py`), so concurrent requests cannot oversubscribe
the CPU.

### 3. Reranking (`qa/rerank.py`)

A cross-encoder (BAAI/bge-reranker-v2-m3) can reorder the top candidates. Its
cost is very hardware dependent: on a CPU-only host it took 4 to 10 seconds per
question for a small gain in ranking. So `QA_RERANK_MODE=auto` measures the
host at start-up and reranks only if the candidates fit in
`QA_RERANK_BUDGET_MS`; otherwise the fused order is used. `on` forces it and
`off` skips loading the model. Exact lookups are never reranked.

### 4. Evidence assembly (`qa/context.py`)

Provisions are included in rank order within both a character and a passage
budget. If the best provision is too large, the matched part and its nearest
neighbours are selected in reading order. A single oversized part is excerpted
for generation and verification. Three kinds of related text can fill the remaining space.

- the definitions section, when the question uses a defined term;
- provisions the selected text refers to ("as provided in section 4");
- provisions that refer to the selected text by number. This is how a
  question about the consequence of breaking a rule finds the penalty clause,
  which typically says only "whoever contravenes sections 18 to 22".

A cross-reference must match the target's provision kind. References attributed
to another Act or Ordinance are excluded from both directions of the map.

### 5. Generation (`qa/prompt.py`, `qa/llm.py`, `qa/service.py`)

One structured call returns `status`, `answer`, and a list of
`{evidence_id, quote}`. The prompt tells the model to answer only from the
numbered evidence, to work out what an informally written question means, to
reply in the user's language, and to treat everything in the user message as
data. Small talk, "which law do you mean?" and "that section does not exist"
are answered without a model call.

When retrieval looks weak for a question with no law or section named (for
example Roman Urdu), the model is first asked to restate the question in plain
English and the search is repeated. The better result is kept. Restatement,
answer generation, correction and provider failover share the time left in
`QA_REQUEST_TIMEOUT_SECONDS`, with time reserved to return the response.
`QA_REWRITE_TIMEOUT_SECONDS` caps restatement at six seconds by default.

The client coordinates retries and concurrent attempts within that budget.

- Each provider has its own time cap within the remaining request budget.
- A timeout, 429, 5xx or malformed output can trigger a retry after a short
  growing pause.
- A call that has not answered after `QA_LLM_HEDGE_SECONDS` can start another
  attempt, alternating between the primary and fallback models.
- At most three calls compete for an answer at once. Per-model failure or
  refusal leaves other models eligible. A credential rejection ends the exchange.
- Each model gets at most one extra attempt after its rate-limit wait. The
  scheduler caps its cooldown at 900 seconds and preserves the provider's
  original wait for the error message. Older results cannot shorten a current wait.
- When every model is rate limited past the deadline the exchange returns
  promptly. An eligible Gemini failure can fall back to a configured OpenAI
  client within the remaining budget. Pending attempts are cancelled and awaited.

If no model can be reached, a request for named provisions is still answered
with status `partial`. It copies each selected published display passage
verbatim below its cited heading and names any missing requested provisions.
For topical questions, direct search hits are checked for overlap between the
question's repaired subject terms and the provision's operative wording. If a
matching excerpt has source mappings for all its wording, the response quotes
it with a citation for each contributing PDF page and status `partial`.
The response identifies the missing explanation. If generation fails and there
is no supported passage, the service returns `service_unavailable` (HTTP 503).

The API loads local model directories. A missing or stalled query encoder lets
keyword retrieval proceed after at most `LEGAL_SEARCH_EMBEDDING_TIMEOUT_SECONDS`
per retrieval (three seconds by default), capped at half the remaining answer
budget. Expanding the search to other laws reuses the same query representation.
Reranking also yields to the
existing retrieval order when its time budget expires.

### 6. Citation verification (`qa/citations.py`)

The model's output is not trusted. Each quote is located in the chunk it
claims to come from, tolerant of whitespace, case, quotation marks, dashes,
PDF footnote numbers and small copying slips, and mapped through the stored
spans to a provision and a page. The response quotes the law's own wording
from the published text, never the model's version.

- A quote that cannot be found is dropped; the answer is kept.
- A cited passage with no usable quote is cited through the sentence that best
  matches the statement it supports.
- A provision the answer names but does not cite ("under Section 31 ...") is
  linked to its text.
- Citation numbers follow the order of first use in the answer.
- Markers after a full stop and adjacent markers attach to the preceding
  statement. List numbers, legal abbreviations and nested indentation survive
  formatting. Unicode digits and English number words help match quotes in a
  translated answer. A fallback excerpt ends on a word boundary.
- Only referenced citations are returned. An insufficiency response clears
  model markers along with its citations. Brackets attached to statute text
  such as `section 3[59]` are preserved.

An answer that still has no verifiable citation after one corrective attempt
is returned with a warning in `limitations` rather than discarded.

## Cross-cutting

| Concern | Where | Behaviour |
|---|---|---|
| Configuration | `core/config.py` | One typed settings class; values are validated at start-up; unknown variables are ignored so older `.env` files keep working. |
| Request handling | `api/middleware.py` | Validated request IDs, security headers, body-size limits on declared and streamed bytes, and a loopback gate for administration. Access logs use the matched route template. |
| Access control | `api/deps.py`, `core/ratelimit.py` | Optional API keys are compared in constant time. The sliding-window limit uses the client address supplied by the ASGI server. Clients behind a shared proxy can share a bucket, depending on the server's trusted-proxy configuration. |
| Admission control | `api/routes/qa.py` | At most `QA_MAX_CONCURRENT_REQUESTS` questions at once; others wait up to `QA_QUEUE_TIMEOUT_SECONDS`, then get 503. |
| Health | `api/routes/health.py` | `/health/live`, `/health/ready`, `/health`. |
| Logging | `core/logging.py` | Text or JSON lines. Questions, prompts, answers and credentials are never logged; each stage logs its duration. |
| Audit trail | `qa/records.py` | Optional rows per question (`QA_RECORD_RUNS=true`): retrieval hits, cited evidence, model name. |
| Failure modes | everywhere | Database down at start-up: the API starts and reports not ready. Database trouble during a refresh: the snapshot in memory keeps serving. An inconsistent publication is rejected and nothing is served until a valid one is active. Model down: see above. |

## Not in scope

The evidence gate (a separate model judgement of whether the evidence is
sufficient before answering) is not part of the pipeline. The answer model
reports insufficiency itself through `status`, and unsupported quotes are
removed by citation verification.
