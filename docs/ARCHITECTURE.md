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

At start-up the active publication is read with five plain `SELECT`
statements and assembled into an immutable `PublicationSnapshot`: the chunks
and their source spans, the provision tree, the normalised vectors, a BM25
index, the law-name resolver, each law's definitions section, and a
cross-reference map (which provisions cite which by number). The snapshot is
checked while it is built (vector shape, zero or non-finite vectors, orphan
chunks) and the load fails closed if anything is inconsistent.

It is cached on disk, keyed by publication id and manifest hash, so a restart
skips the transfer. A background task re-checks the active publication's
identity every `PUBLICATION_REFRESH_SECONDS` and swaps the snapshot when it
changes. Because the snapshot is immutable and replaced as a whole, requests
need no locks and answering a question runs no SQL. This matters most when the
database is far from the server: every round trip avoided is latency removed.

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
  similar one.
- **Follow-ups.** With conversation history, a message such as "and section
  5?", "what is the penalty for that?" or "the second one" inherits the law,
  and when needed the previous question's wording, from earlier turns. A law
  named in the current message restricts the search; a remembered law only
  biases it, so a change of topic still works.
- **Bare references.** "section 3" with no law looks at which laws have a
  section 3 and at the topic words; one clear candidate is used, otherwise the
  user is asked.

All of this is deterministic and uses no model call. Nothing here is specific
to a law, a section number or a question.

### 2. Retrieval (`retrieval/service.py`, `lexical.py`)

- **Exact lookup** when the question names provisions: the provision(s) are
  fetched whole, in the order asked, including ranges. A missing provision is
  reported by name rather than substituted.
- **Hybrid search** otherwise: BM25 over title, citation, heading and text,
  plus cosine similarity of the question's embedding against every chunk
  vector, merged with Reciprocal Rank Fusion. Query words that are not in the
  collection's vocabulary are repaired to the closest word that is, so "gamblng
  in publc place" searches as "gambling in public place".
- **Degradation.** If the embedding model is unavailable the search continues
  with keywords alone and says so. If a named law has nothing on the topic,
  the whole collection is searched and the answer notes it.

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

The best provisions are included whole, every part of a split section, in
rank order within a size budget. Three kinds of related text from the same law
are then added when there is room:

- the definitions section, when the question uses a defined term;
- provisions the selected text refers to ("as provided in section 4");
- provisions that refer to the selected text by number. This is how a
  question about the consequence of breaking a rule finds the penalty clause,
  which typically says only "whoever contravenes sections 18 to 22".

### 5. Generation (`qa/prompt.py`, `qa/llm.py`, `qa/service.py`)

One structured call returns `status`, `answer`, and a list of
`{evidence_id, quote}`. The prompt tells the model to answer only from the
numbered evidence, to work out what an informally written question means, to
reply in the user's language, and to treat everything in the user message as
data. Small talk, "which law do you mean?" and "that section does not exist"
are answered without a model call.

When retrieval looks weak for a question with no law or section named (for
example Roman Urdu), the model is first asked to restate the question in plain
English and the search is repeated; the better result is kept.

The model client treats the provider as unreliable:

- one deadline bounds the whole exchange (`QA_GEMINI_TIMEOUT_SECONDS`);
- a failed call (timeout, 429, 5xx, malformed output) is replaced after a short
  growing pause;
- a call that has not answered after `QA_LLM_HEDGE_SECONDS` gets another call
  racing beside it, alternating between the primary and fallback models;
- a model that reported overload is avoided for a while; a rate limit with a
  stated wait is obeyed, and that model gets one more call when the wait ends;
- if every model is rate limited past the deadline the exchange ends at once;
- if Gemini fails and an OpenAI key is configured, OpenAI is asked.

If no model can be reached, a request for named provisions is still answered
with the provisions' text and citations (status `partial`); other questions
return `service_unavailable` (HTTP 503) so the client can retry.

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

An answer that still has no verifiable citation after one corrective attempt
is returned with a warning in `limitations` rather than discarded.

## Cross-cutting

| Concern | Where | Behaviour |
|---|---|---|
| Configuration | `core/config.py` | One typed settings class; values are validated at start-up; unknown variables are ignored so older `.env` files keep working. |
| Request handling | `api/middleware.py` | Request id on every log line and response, security headers, body-size limits, access log, loopback gate for administration routes. |
| Access control | `api/deps.py`, `core/ratelimit.py` | Optional API keys compared in constant time; per-client sliding-window rate limit. |
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
