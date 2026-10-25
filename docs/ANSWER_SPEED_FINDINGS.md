# Answer speed findings - 23 October 2026

Measured only in `D:\PART C SHIMZA FYP\legalmind-backend`, on `shimza-phase-c`.
No OpenAI requests, corpus edits, migrations, or database writes were made.
During the baseline measurements below, application logic and the frontend
timeout were not changed. The later Part C fixes are documented separately below.

## Confirmed measurements

| Operation | Measured time | Detail |
| --- | ---: | --- |
| Initial database connection/check | 29.365 s | First profiling process; a second connection took 8.476 s. |
| Exact-reference retrieval, Step 10 | 92.568 s | 13 SQL statements; 77.494 s inside database execute/fetch calls. |
| First reranker request, Step 11 | 71.186 s | Includes 67.513 s loading/importing the cached model. |
| Same reranker after loading | 3.481 s | Same candidates and same model instance, no extra retrieval. |
| Context expansion, Step 12 | 107.682 s, failed | Raised `DBAPIError` before the context was complete. The original SQLSTATE was not captured in this first run. |
| Context catalog alone, separate process | 94.045 s, passed | 8 SQL statements; 90.830 s inside execute/fetch. Loaded 23 instruments, 531 chunks and 2,815 provisions. |

These are two separate read-only diagnostic runs, not timings captured inside
the earlier failed browser request. The first run took 304.753 seconds,
including the additional warm-reranker comparison, and stopped before any LLM
call. It did not reach source verification. The second probe took 112.509
seconds and successfully loaded the catalog. The first database error was not
reproduced in the second probe, so a statement timeout, query defect or network
failure must not be claimed as its exact cause yet.

Simple repeated `SELECT 1` calls in the second probe took 0.963-1.738 seconds
each. `EXPLAIN ANALYZE SELECT 1` reported 0.021 ms server execution and 0.016 ms
planning, while the client call took 1.249 seconds. This demonstrates significant
connection/driver/network round-trip overhead for that simple query. It does
not isolate server execution time for the larger legal queries.

## Relevant code and next work

- `app/repositories/legal_search_repository.py`: Step 10 performs two separate
  publication-scoped snapshots, completeness/catalog checks and exact/source
  retrieval. This is Abdullah's frozen interface; no logic was changed. Its
  measured database time warrants coordinated profiling with him, without
  removing publication/completeness safeguards.
- `app/services/qa_reranker.py`: the lazy Step 11 model load accounts for nearly
  all of the first reranker's time. The cached model itself ranks this one
  candidate in about 3.5 seconds. Loading it once before accepting questions
  would move this first-use wait out of the question's response time.
- `app/repositories/qa_context_repository.py`: `snapshot()` loads all published
  items, chunks, provision nodes, memberships and references. Step 12 pays for
  that full catalog even for a question about one section.
- `app/services/qa_source_verification.py`: every `verify()` calls that full
  snapshot loader again. `app/services/qa_citations.py` calls `verify()` per
  claim. This repeated full-catalog work is a code finding; source-verification
  latency was not separately measured because the first run stopped earlier.

The first Part C optimization should be publication-pinned, targeted context
and proof reads, with one fresh verification snapshot per answer rather than
one complete corpus reload per claim. Preserve active-publication checks,
source identity, complete required references/qualifiers and exact span proof.
Avoid holding the context-read transaction open through an external LLM call
or globally caching stale source graphs. Measure the change before choosing a
frontend wait window. Increasing the wait window alone does not reduce work.

## Reproduce without an AI call

`scripts/measure_answer_speed.py` is the only new executable file. It injects
timers around existing services, blocks database write statements, disables
OpenAI/audit writes in its own process, and uses cached local model files.
It reports only stage names, durations, counts, exception types and sanitized
SQLSTATE/category fields. It never prints question excerpts, answers, SQL
parameters or configuration secrets.

```powershell
.\.venv\Scripts\python.exe -u -m scripts.measure_answer_speed
.\.venv\Scripts\python.exe -u -m scripts.measure_answer_speed --database-only
```

Raw, non-secret timing reports are in ignored local files:

- `tmp/answer-speed-20261023T133504Z.json`: first pipeline measurement.
- `tmp/answer-speed-20261023T133904Z.json`: database/catalog probe.

No code was committed or pushed. These measurements identify runtime
bottlenecks; they are not legal-accuracy tests or a successful live answer test.

## Part C changes and read-only verification

Implemented locally on 23 October, only in this checkout:

- `qa_context_repository.py` projects the publication identity fields instead of
  loading whole revision/report records. It still checks the active publication,
  total published chunk count, approval, hashes and pinned source identities.
  It initially loads the selected instruments' graphs, and loads another law
  only for a stored resolved reference. It keeps complete provision hierarchies,
  split roots, definitions, qualifications and dependency checks. Source reads
  are batched and cached only inside the current read-only transaction.
- `qa_context.py` supplies the selected instrument IDs and batches the exact
  sources for each complete context group. Required/optional dependency and
  budget behavior is unchanged.
- `qa_citations.py` validates all claim IDs, quotations and versions before
  sending them to one `qa_source_verification.py` batch. Verification opens a
  **new post-generation read-only snapshot**, reads only the quoted chunks or
  provisions and checks every quoted character against stored PDF block spans.
  Proofs are not cached across answers or across an LLM call.
- `qa_reranker.py` can preload its existing singleton from the installed local
  model snapshot, without scoring a query, downloading weights or calling AI.
  The small `app/main.py` integration hook awaits this warmup when legal search
  is enabled. `QA_RERANK_WARM_ON_STARTUP=false` restores lazy loading if needed.
  Missing model files still fail honestly; no substitute scores are generated.

No Haroon ingestion logic, Abdullah retrieval/chunking/embedding/publication
logic, frontend behavior, database schema, corpus data or `.env` was changed.
Startup warmup moves the model-loading wait to startup; it does not eliminate it.

The real read-only profiler report is
`tmp/answer-speed-20261023T140422Z.json`:

| Stage | Time | Result |
| --- | ---: | --- |
| Database initial connection | 16.102 s | Passed |
| Unchanged upstream exact retrieval | 40.125 s | 1 candidate, 13 statements |
| Reranker startup load | 59.894 s | Cached local model, no download |
| First rerank after warmup | 4.535 s | Same loaded model |
| Repeated warm rerank | 4.023 s | Same frozen candidate |
| Scoped context expansion | 80.345 s | 11 statements; ready for prompt |
| Five quotation ranges verified in one batch | 12.831 s | 6 statements; all 5 proofs passed |

Context loaded **1 law, 42 chunks and 208 provisions**, while retaining the
lightweight publication catalog of 23 laws. The previous full graph contained
531 chunks and 2,815 provisions. The evidence gate returned `ready_for_prompt`
with no critical issues. The optional incomplete definition remained omitted.

These measurements were taken at different times over a variable database
connection. The earlier 94-second measurement was catalog loading alone, and
the earlier full context run failed; neither is an identical completed-context
benchmark for calculating a percentage speedup. The reduced graph scope and
single verification batch are verified structural improvements. Context and
upstream retrieval are **still slow** on this connection. The 218.924-second
diagnostic total includes startup and the extra warm-rerank comparison, and
excludes any AI generation time. A complete browser answer has not been retested
with OpenAI after these changes. No legal-accuracy claim is made.

Validation: **443 backend tests passed, 1 OCR test skipped** because local
Tesseract English data is missing. **14 frontend browser tests passed** using
explicitly simulated responses. The tests include late-response cancellation,
loading/errors, malformed citations, publication/source changes, batched proof
reads, lazy cross-law references and model reuse. All 184 Git-eligible Python
files parse. The configured secrets were absent from Git-eligible files in both
repositories; `.env`, `.env.local`, `.venv` and `tmp` remain ignored. A before/after
hash check found no new tracked-source changes except the four-line startup hook
in `app/main.py`; earlier uncommitted work remains intact. Both local URLs
returned HTTP 200 after reload. Everything remains uncommitted.
The frontend also passed its real-backend browser check for nonexistent Section
9999, returning the honest unsupported state. The real AI browser test was
explicitly disabled and skipped, so this adds no AI-generation verification.

## Conservative read batching - 24 October 2026

This change groups existing Part C reads while retaining the same context
selection and complete graphs for the selected laws. Only
`app/repositories/qa_context_repository.py` changes application behavior:

- One SQL statement now reads the active publication, completeness count, and
  pinned source identities. An outer join preserves the empty-publication
  rejection. Approval, version, and hash validation still cover every item.
- Each chunk query also reads its provision membership as an aggregated UUID
  set. Chunk text is returned once per chunk, and missing/cross-revision members
  still fail the existing validation. Provision and reference reads retain their
  existing selection and ordering.

Every context/proof read retains its own repeatable-read, read-only transaction.
Source proofs continue to use a fresh post-generation snapshot. The context
expander, evidence gate, prompts, models, budgets, citation rules, and API
contracts are unchanged. No cross-request cache is introduced.

Read-only comparisons loaded the original repository from an immutable local
copy and compared it with the changed code against the same active publication.
The complete published graphs matched: **23 instruments, 531 chunks, 2,815
provisions, and 516 references**, including each source's reference order. For
the same frozen, real retrieved/reranked Section 19 candidate, the expanded
context, evidence decision, and exact prompt also matched. Five quoted source
ranges produced identical proofs in fresh snapshots.

Three measurements per version alternated the before/after order:

| Operation | Before median | After median | SQL statements before / after |
| --- | ---: | ---: | ---: |
| Context expansion | 17.093 s | 13.435 s | 11 / 9 |
| One verification batch containing five ranges | 10.068 s | 8.432 s | 6 / 5 |

The complete-graph comparison used 8 statements before and 6 after. Network
timings varied: one context pair was slower after the change, while the other
two were faster. These are small, controlled read-stage measurements, not a
new end-to-end AI/browser latency result. They must not be subtracted directly
from an earlier 87-second request to predict a new total.

Validation: **450 backend tests passed, 1 OCR test skipped** because local
Tesseract English data remains unavailable. Added regressions cover empty and
invalid publications, later invalid items, complete graph retention, split
sections/qualifiers/definitions/references, missing or cross-revision members,
and null/duplicate membership handling. No additional OpenAI request, migration,
database write, frontend/configuration change, or Phase A/B source change was
made. Local originals and raw comparison evidence are retained under ignored
`tmp/qa-batching-20261024/`.
