# Shimza's Phase C: answering from approved PDF evidence

`POST /ask` accepts `{ "question": "Section 19 of the Consumer Protection Act" }`.
It uses Abdullah's active-publication retrieval, then reranks, expands context,
checks whether enough source-backed text is available, asks the configured LLM
for a structured draft, verifies quotations against the stored PDF extraction,
and returns only verified quotations and real source metadata.

The response status is `answered`, `partial`, `clarification_needed`,
`insufficient_evidence`, or `service_unavailable`. A `partial` answer says that
the available PDFs do not establish a complete answer, while showing a cited,
possibly relevant quotation. With no useful evidence, the API says it cannot
determine the answer from this limited collection. It does not claim that the
law itself has no answer, invent a plausible rule, or make an LLM call after an
evidence-gate rejection. Model prose is not exposed as an unchecked legal claim.

## Local setup

1. Keep `DATABASE_URL` and `OPENAI_API_KEY` in the ignored backend `.env`.
   Rotate any key that was pasted into chat. Restart the server after rotation
   because settings and the API client are cached per process.
2. Set `LEGAL_SEARCH_ENABLED=true`, `OPENAI_MODEL=gpt-4o-mini` (or another
   supported model), and an allowed `CORS_ORIGINS` in `.env`.
3. Install dependencies and run the API from this backend directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs`. A terminal request uses:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/ask' -ContentType 'application/json' -Body '{"question":"Section 19 of the Consumer Protection Act"}'
```

The first reranker/model request may take time to load local weights. The
`BAAI/bge-reranker-v2-m3` reranker is separate from Abdullah's unchanged
`BAAI/bge-m3` retrieval embeddings. For a read-only smoke check that makes no
LLM call and no database write:

```powershell
.\.venv\Scripts\python.exe -m scripts.check_qa_readonly
.\.venv\Scripts\python.exe -m scripts.demo_phase_c_readonly
.\.venv\Scripts\python.exe -m scripts.check_qa_api_no_key
.\.venv\Scripts\python.exe -m pytest -q
```

The API no-key check disables the provider key and audit writes inside its
Python process before importing the app. In PowerShell, assigning `''` to an
environment variable removes it, so a `.env` value would otherwise be loaded.

After replacing any exposed key, one live answer check can be run with
`.\.venv\Scripts\python.exe -m scripts.demo_phase_c_live`. It disables audit
writes, contacts the configured provider, and prints only outcome and citation
counts/pages. A cited response is a pipeline check, not a legal accuracy test.

`scripts/run_retrieval_tests.py` is Abdullah's six-query Stage 10 suite. It
writes `backups/retrieval_test_results.json` locally and may download the large
semantic embedding model for non-exact questions. That file is ignored by Git.

## Audit migration

`20261017_18_phase_c_records.py` prepares only `qa_runs`, `retrieval_hits`,
`answer_claims`, and `answer_evidence`. It has **not** been applied to the
external/shared database. `QA_RECORD_RUNS=false` by default, so `/ask` remains
read-only with respect to that database. After reviewing the migration and
confirming the target database, apply it separately and then set
`QA_RECORD_RUNS=true` if audit persistence is desired. No Phase A/B tables or
published source content are modified by this feature.

Citation verification checks stable IDs, publication/version identity, exact
quoted characters, chunk/provision-to-PDF block mappings, and real page and
source metadata. A verified quotation still does not prove legal applicability
to a user's facts; the response states this limitation.

## Part C corrections — 22 October 2026

All corrections are in this checkout (`D:\PART C SHIMZA FYP\legalmind-backend`)
on `shimza-phase-c`. Haroon's ingestion/parser logic and Abdullah's retrieval,
chunking, embeddings, publication code, and tables are unchanged. The local
pre-edit snapshots of the `qa_*` implementation and test files are preserved in the ignored
`tmp/part-c-fixes-20261022/before/` directory.

- **Complete context:** Retrieved sections, referenced sections, and discovered
  definitions now use the same expansion logic. Their complete published
  fragments, parents, provisos, and explanations stay together. A reference to a
  nested clause expands its whole published section. References inside added
  definitions and provisos are followed too. Optional definitions are omitted
  as a whole when they cannot fit; a missing required source or unresolved
  required reference stops generation. The 23 October correction below also
  treats optional reference dependencies atomically. Non-operative qualifier
  ancestry is excluded.
- **Paraphrases:** A candidate admitted by Phase B's dense-retrieval filter
  can reach grounded model review when its cross-encoder score is positive,
  even without shared query words. A raw reranker score alone cannot do this.
  Exact-reference and lexical-only routes remain available. These relevance
  heuristics are not legal confidence scores; corpus evaluation is still needed.
- **Missing user facts:** After the source/relevance gate accepts evidence, the
  structured answer step can choose `clarification_needed`. It must supply one
  factual question, a description of the missing information, and quotations
  supporting why the question is being asked. Those quotations must pass the
  same publication/version/PDF-span checks as an answer before the API returns
  them. This model-assisted identification of a missing fact still needs human
  accuracy evaluation. The earlier reviewed `NecessaryFact` gate interface is
  retained; no fabricated legal-rule registry or database change was added.
- **Partial answers:** The specific `missing_support` detail is returned in its
  own response field and in `limitations`, labelled as a model-identified gap.
  It is not a verified legal conclusion. An `answered` draft cannot also claim
  missing support, and a `partial` draft must explain its gap.
- **Follow-up connection:** Send a factual reply as `question`, and the earlier
  question as `context.previous_query`. Part C combines these explicit inputs
  before calling Phase B, preserving an optional `context.law`/`instrument_id`.
  No server-side chat history or modification to Abdullah's parser is needed.
  The combined input must fit 4000 characters; invalid input returns HTTP 422.

The generator keeps the existing Responses API adapter and the configured
model. The nullable clarification field follows the
[official OpenAI Structured Outputs contract](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
No key or `.env` setting was changed, and no live OpenAI call, migration, corpus
edit, or audit-table write was made for these corrections. Test provider
responses and source graphs are synthetic; they are not legal accuracy results.

The earlier expected-failure test covered missing facts only in pre-generation
preparation. It is replaced by a passing public `/ask` integration regression
covering preparation, structured clarification, source verification, and JSON
output. Other regressions cover missing exceptions, split reference targets,
context budgets, paraphrases, missing/forged support, partial details, and factual
follow-up inputs. The 20 corpus questions remain a separate live/manual evaluation.

Verification in this checkout: **412 passed, 1 skipped, 0 failed, 0 expected
failures** (`python -m pytest -q -p no:cacheprovider`). The skipped test needs
local Tesseract English OCR data and belongs to extraction. `pip check` found
no broken requirements. Before-edit hashes confirm no tracked source or
migration file changed during these corrections. Existing tracked edits from
earlier work are preserved. The checks use no shared database or paid API calls.

## Optional definition correction - 23 October 2026

This correction is only in `D:\PART C SHIMZA FYP\legalmind-backend`, on
`shimza-phase-c`. No changes were made to Haroon's or Abdullah's core logic,
the shared database, published chunks, reference records, or the private `.env`.

The earlier expansion treated an unresolved reference inside a heading-selected
definition as a fatal problem for every question about that law. This blocked
even the request to read Consumer Protection Act Section 19. Expansion now
finishes retrieved sections and their required reference dependencies first.
It then attempts each optional definition with its entire dependency group.
If any required part of that optional group is unresolved, outside the pinned
publication, missing source spans, too deep, or over budget, the whole group is
removed. Existing required passages and shared parents are retained, and the
budget counters are restored. The omission is recorded explicitly in the prompt.

A definition referenced by the requested provision remains required, even when
its heading would also qualify it as an optional definition. Missing required
references or exceptions still stop generation. Only stored, resolved reference
targets are followed; no database target is guessed or silently approved.
The prompt explicitly forbids guessing omitted definitions and requires a
partial or insufficient-evidence response when the missing context matters.

Files changed in this correction:

- `app/services/qa_context.py`: prioritize required context and roll back incomplete
  optional definition groups, including their reference dependencies.
- `app/schemas/qa_context.py`: record `optional_context_omitted` explicitly.
- `app/services/qa_prompt.py`: forbid inferring omitted context.
- `tests/test_qa_context.py` and `tests/test_qa_prompt.py`: 12 added regression
  cases for optional omissions, strict required references, shared context,
  cycles, budget restoration, and prompt evidence.
- `scripts/demo_phase_c_readonly.py`: exit unsuccessfully when its evidence gate
  or stored source-span proof fails, rather than reporting a misleading success.
- This document: explain the behavior and validation limits.

The final isolated backend suite reports **424 passed, 1 skipped**. The skipped
test needs missing Tesseract English OCR data. `pip check` reports no broken
requirements. All 182 Git-eligible Python files parse successfully. Tracked-file
hashes confirm existing tracked changes were preserved; this correction changes
only the listed, already-untracked Part C files. Configured secrets were not
found in Git-eligible files, and `.env`, `.venv`, and `tmp` remain ignored.

With the current real publication, Section 19 now reaches `ready_for_prompt`,
and its stored extraction source-span proof passes. The incomplete optional
definition is omitted and noted. The user's updated configuration passes the
official OpenAI model-access check; no configuration values were printed.

The real `/ask` route, exercised in-process with FastAPI TestClient and the
current corpus/provider rather than mock responses, returned **HTTP 200,
`answered`, 1 verified claim, and 5 citations on physical PDF page 7** for
Consumer Protection Act Section 19. The OpenAI Responses request also returned
HTTP 200. Separate API checks with the key disabled in that test process passed:
empty question -> HTTP 422; nonexistent Section 9999 of the Consumer Protection
Act -> HTTP 200 `insufficient_evidence`, no citations; unqualified `Section 19`
-> HTTP 200 `clarification_needed`, no citations. OpenAPI returned HTTP 200.
These negative checks made no AI requests. No shared database writes were made.

The backend was then started from this checkout on `http://127.0.0.1:8000`
with reload enabled and audit writes disabled. Real HTTP requests returned
200 for `/openapi.json` and `/docs`; the schema documents `POST /ask`.
The preflight for `http://localhost:3000` returned 200 with that allowed origin.
Server output and its launcher PID are in ignored `tmp/part-c-server-20261023/`.
At that point no frontend app existed here. A local testing frontend was added
later in the sibling `legalmind-frontend` folder and is available at
`http://localhost:3000`; see its README for setup and manual checks.

The subsequently approved one-question AI browser test hit its four-minute
answer deadline while the page still showed loading at 239 seconds. OpenAI
returned HTTP 200 according to this server's log, but a complete response and
its citations were not verified in that browser run. The earlier successful
in-process answer test remains a separate result. Runtime latency and the
frontend wait window need investigation; no second AI question was submitted.

These checks cannot certify legal correctness. Original source PDFs remain
absent from this checkout. The frontend testing app has since been added; its
synthetic UI tests and real unsupported-question check are separate from the
earlier live backend answer test. Human PDF review,
upstream reference resolution and qualifier-only retrieval coverage remain
separate corpus/handover work. Optional answer-record tables have not been
created; audit writes remain disabled until a database migration is approved.

## Answer-speed corrections, 23 October 2026

Part C now reads only the selected laws' context graphs, loads resolved cross-law
targets on demand, and batches/caches context-source reads within one read-only
transaction. Citation claims share one fresh, scoped post-generation proof
snapshot, preserving exact block/character checks and avoiding a corpus reload
per claim. The cached reranker is loaded at startup by a small `app/main.py`
integration hook; its singleton is reused for questions. These changes touch
only owned Part C modules/tests plus that hook. Haroon's and Abdullah's logic and
all shared database data/schema remain unchanged.

The isolated suite now reports **443 passed, 1 skipped**, and all **14 synthetic
frontend browser checks passed**. Real read-only measurements verified context
for one law (42 chunks/208 provisions) and five source ranges in one proof batch.
No OpenAI call was made for this correction. Context preparation and upstream
retrieval remain slow on the current connection, and a complete AI answer in the
browser has not been retested after the correction. Details and limits are in
`docs/ANSWER_SPEED_FINDINGS.md`. No code was committed or pushed.
The real-backend unsupported-question browser check also passed; its separate
OpenAI answer test was explicitly disabled and skipped.

## Step 12 database context expansion - 24 October 2026

Phase C consumes the approved database extraction and the active publication's
retrieval candidates. It does not require original-PDF files in the repository.
Roman Urdu support is outside this implementation's current scope.

Context expansion retains complete published section fragments, parent text,
provisos and explanations. Definitions remain optional whole dependency groups;
required source context is never silently truncated or made optional.

References can now be followed through either a stored resolved target or a
strict, read-only same-revision structural lookup. The latter uses the stored
operative wording, its exact extraction spans, and a unique numbered target in
the same published law. It does not mark a database reference reviewed, change
the corpus, or infer legal applicability. `ContextResult.reference_links`
records the source provision, exact mention offsets, target identity, and
whether the connection came from a stored link or a same-revision lookup.

The lookup handles full numbered lists and simple subsection selectors, even
when ingestion saved only a list's first number. Exact labels distinguish, for
example, Section 14 from 149 or 14A. Definitions that explicitly define “Act” as
the current instrument can disambiguate that term. Explicit external-law
mentions, competing scopes, duplicate targets, rejected/ambiguous reviews,
unverified amendment-marker syntax, ranges and unsupported compound selectors
remain unresolved. Reviewed cross-law links still require a target in the
same active publication. Target conditions, source proofs, cycle detection and
the existing depth/token/character/unit limits continue to apply.

An incomplete optional definition group rolls back both its passages and its
reference audit. Incomplete required dependencies still produce a critical
issue for Step 13; no answer-generation call is made after that rejection.
Consequently this change does not promise that every existing corpus gap is
resolved. A clear link may be found while its full dependency group remains
too large or too deep for the configured context budget.

Verification: the backend regression suite passed **509 tests, with one
existing OCR skip**. New synthetic tests cover internal and reviewed external
references, complete lists, nested selectors, label collisions, ambiguous
scope, amendment markers, exact source provenance, missing targets, budgets,
depth, split sections, qualifiers and atomic optional rollback. Real database
checks and their scope are recorded under `tmp/step12-20261024/`. No migration,
database write, PDF addition, environment change or paid AI request was made.

## Step 13 evidence gate - 24 October 2026

The pre-generation gate now checks that selected candidates and retained
retrieved passages have matching chunk/root identities, publication, legal
version, revision, chunk set and text. Evidence IDs must be unique and match
their source kind. Every retained passage needs exact source metadata with
valid span/file/block IDs, a SHA-256 identity and a positive page number.
Conflicting versions, missing conditions and critical Step 12 omissions still
stop generation. An optional definition's omission alone does not block an
otherwise eligible request.

Relevance passes before any personal fact question is considered. Exact and
semantic signals therefore belong to passages actually retained. Lexical
overlap can combine split fragments of one section, but cannot manufacture a
match by pooling isolated words from unrelated sections. These are eligibility
heuristics, not legal confidence scores or proof of applicability.

`NecessaryFact` now requires an exact supporting `quote` as well as its evidence
IDs. Requirements with unavailable text, unsupported quotations or duplicate
fact keys are rejected, including when a fact is marked known. A missing fact
can produce a pre-AI follow-up only after source/relevance checks pass. The
question and condition are bound to that reviewed requirement, and the normal
citation verifier freshly checks the source before the question is returned.
That route performs no LLM generation or repair call.

Reviewed requirements can be supplied through the server-only
`FactRequirementProvider.assess(request, context)` interface, returning a
`FactAssessment`. Register a provider as
`app.state.qa_fact_requirement_provider`; the API dependency supplies it to
question preparation. A provider must use reviewed rules, match the question
and pinned source version, and identify known facts only from this request's
explicit information. A quotation alone does not prove that a fact is legally
necessary. `/ask` does not accept client-supplied rule requirements or known-fact
keys that could bypass these checks. Provider failure returns a service error.

**Current data limit:** no reviewed fact-rule provider is registered by default,
and this change does not invent a legal-rule registry or infer necessary facts
from keywords. Existing source-grounded AI-assisted factual clarification can
still occur after the evidence gate has accepted the source context. Automatic
pre-AI discovery of every necessary user fact is not claimed to be implemented.
Retrieval ambiguity can still be clarified before generation without a provider.

Verification covers the public API's cited no-AI follow-up, the known-fact path,
fresh source failures, rejection paths, candidate/source mismatches and lexical
grouping. The 16 saved English database contexts retain their previous gate
outcomes. Final suite results and review artifacts are recorded under
`tmp/step13-20261024/`. No corpus, migration, environment setting, PDF storage,
Roman Urdu feature or paid AI request was changed for this work.

## Step 14 prompt construction - 24 October 2026

The builder now rechecks the local Step 13 evidence contract before creating a
prompt. A copied ready status cannot bypass critical omissions, invalid source
metadata, mismatched selected passages or publication/version checks. The
approved evidence IDs must uniquely match all retained units; the builder does
not select a smaller subset or silently remove conditions. A reviewed fact
clarification cannot be relabelled as a ready answer decision.

The question and resolved follow-up question are included with all retained
source text, unchanged. Parent text, complete section fragments, qualifiers,
definitions and followed references retain their evidence identities, labels,
paths, edition, jurisdiction, applicability and ancestor labels. Context notes
now include the omission code and source identity as well as the explanation.
Those notes are not citable source passages and cannot replace missing law.

Source metadata is explicitly selected. Filenames and URLs are omitted from the
model prompt; the server still resolves them for verified final citations.
The static instructions require source-only quotations, the structured answer
fields, preservation of conditions, and explicit partial/insufficient outcomes
when necessary support is unavailable. Questions, extracted text, headings and
notes remain untrusted data in the user message. Server repair feedback may
identify a draft error but cannot introduce evidence or relax these rules.

The builder checks nonempty passages, actual retained character counts and
the context's character/unit/reported-token budgets. It also caps the serialized
system and user strings at 128 KiB in UTF-8. Oversized or inconsistent contexts
fail before generation; required text is never truncated. This byte cap is a
serialization safeguard, not provider token counting, a model-window guarantee
or a budget for the response schema, output tokens and later repair feedback.
Larger metadata can therefore reject a request even when its source text alone
fits the Step 12 budget. The additional checks are local and add no DB reads.

Verification includes synthetic prompt and fake-generator boundary tests plus
an offline replay of 16 saved English database contexts. All nine previously
eligible contexts still build prompts with identical source text and evidence
IDs; the other seven remain blocked. Prompt-injection tests establish message
separation and output verification, not universal model resistance or legal
accuracy. No fresh paid model response was tested. Results and remaining limits
are recorded under `tmp/step14-20261024/`.

The Step 13 reviewed fact-rule data gap and existing corpus/reference gaps
remain. No database, publication, migration, PDF storage, environment setting,
frontend or Roman Urdu feature was changed for Step 14.

## Step 15 answer generation - 24 October 2026

The generation adapter was reviewed using the installed OpenAI SDK, version
2.54.0, with HTTP responses intercepted entirely in memory. Tests exercise the
actual strict-schema serialization and JSON parser without sending requests to
OpenAI or loading a real credential into the test client. The configured model,
output-token allowance, system/user messages and `store=False` are preserved.
No tools or previous provider conversation are attached to generation requests.

Three boundary-handling gaps were corrected in `qa_generation.py`:

- JSON/schema parsing errors raised inside `responses.parse` now produce
  `GenerationInvalid`, rather than being classified as provider outages.
- An explicit refusal anywhere in assistant message content rejects the draft,
  including a mixed response containing both a refusal and parseable JSON.
- Existing `AnswerDraft` objects are converted back to data and strictly
  validated, including nested claims. Objects created or altered through
  `model_construct` or `model_copy` cannot bypass the output schema.

The public failure behavior remains controlled: invalid drafts, refusals,
truncation, unavailable providers and authentication/rate-limit failures return
`service_unavailable`/HTTP 503, without draft claims, citation metadata or SDK
error details. Invalid model output does not mean insufficient legal sources.
Those failures do not trigger an extra model request. The existing bounded
citation repair remains in Step 16; valid schema alone does not establish that
a legal claim is supported. Final answers still contain verified quotations
rather than the model's unverified prose.

Verification: **621 backend tests passed, with one existing OCR skip**,
including 40 new SDK/HTTP/API cases. The new cases cover all structured
outcomes, strict request schema, configured model/output limits, source and
question preservation, malformed JSON, missing/wrong/extra fields, altered
model objects, refusal, noncompleted statuses, HTTP and transport failures,
zero SDK retries, controlled public failures, no-call gate rejection and
bounded citation repair. All nine saved English model drafts re-parse unchanged
through the old and new adapters; the seven blocked English contexts remain
blocked. These are offline replays, not new model answers or live source checks.

The normal app still does not persist provider-returned model IDs, token usage
or generation timing for GN-10 in the broader test ledger. QA run recording is
a separate remaining task and remains disabled by default. A freshly generated
live answer, actual model quality, billing and latency were not tested here.
Earlier corpus/reference and reviewed fact-rule data limits remain unchanged.

Review artifacts are under `tmp/step15-20261024/`. No database write, migration,
publication change, PDF addition, environment setting, frontend change, model
switch, output-budget change or paid AI request was made.

## Step 16 citation verification - 24 October 2026

The citation layer now independently checks full non-whitespace character
coverage for each quoted claim, in addition to the existing fresh source
verifier's checks. An incomplete or unrelated proof cannot produce a claim
with missing citation indexes. Source spans are ordered by their positions;
whitespace-only gaps remain allowed. This applies to ordinary answers and
the source-backed, pre-AI factual clarification path.

Quotation matching still preserves case, punctuation, legal wording and
numbers, with whitespace variation allowed. It now requires whole-word/number
boundaries at the start and end of an alphanumeric quotation. A model cannot
quote `lawful` from inside `unlawful`, or cite a shortened number from inside a
longer one. A later valid whole-word occurrence can still be matched.

Chunk, provision and normalized source-block mappings now need valid integer
offsets within the actual stored strings. Missing, negative, empty, boolean
or oversized ranges are rejected before a mapping is approved. This prevents
Python's permissive slice clipping from making corrupt offsets appear exact.
The provision-source repository uses the same range check, so both source kinds
receive the protection. Source reads remain publication/version pinned and
read-only, with the existing batched queries and fresh-snapshot behavior.

`qa_source_proofs.py` holds the shared proof type, coverage/range checks and
citation error class. The previous imports of `SourceProof`, `covered_proofs`
and `CitationVerificationError` remain available through their original
modules. No public response schema or database migration changed. Two existing
source-change fixtures now use a same-length replacement to isolate text
identity failures; the new range tests separately cover shortened/malformed
mappings. The existing bounded citation repair allowance was not increased.

Verification: **661 backend tests passed, with one existing OCR skip**,
including 40 new citation/source-boundary cases. An offline replay retained all
17 claims and 21 citations from nine saved English answers, with all recorded
claim/citation fields unchanged. Seven other English contexts remain
blocked before generation. The replay reconstructs proof coverage from saved
page-specific quotations and metadata; it is not a fresh live database check.

These checks prove text/provenance consistency within the tested scope. They
do not establish current legal applicability, expert legal interpretation or
that a selected quotation includes every legally necessary condition. Existing
context expansion, evidence gating, prompt instructions and fresh production
source verification remain necessary. Current corpus/reference gaps, reviewed
fact-rule data gaps, usage/timing recording and a new live AI evaluation remain
separate work.

Review artifacts are under `tmp/step16-20261024/`. No original PDF, database
write, publication change, environment setting, frontend change, model switch
or paid AI request was added. Part C continues to use extracted database text.

## Step 12 required-reference precision - 25 October 2026

Required references to a first-level subsection can now use a conservative
structured group below a title-only powers provision. The complete target
subtree, source-backed parent text, recognized qualifications, delegation and
appeal clauses, and intra-section references in either direction remain in
that group. Ordinary conditions and uncertain prose remain included. Only
separate permissive powers without a detected connection or qualification may
be excluded. Unclear selectors, mixed hierarchy, operative parent text,
unrecognized children and other unsupported forms retain the full-root path.
This is a bounded structural/text heuristic, not a legal interpretation engine.

The full published root still needs every indexed fragment and the correct
publication/revision/chunk set. Each retained provision is fetched whole and
needs exact stored source spans. Group insertion remains atomic, with the
existing token/character/unit/depth limits. A scoped cache entry does not
satisfy a later reference to the entire section. Retrieved primary sections
and their split chunks continue to be retained in full.

Verified references to numbered subsections already contained in a complete
group reuse that group without consuming another depth level. This includes
references to a nested paragraph inside a retained subsection. Unverified or
unsupported selectors still use the conservative full-section path; a partial
group cannot satisfy a reference to the whole section.

The new scope applies only to required dependencies. Heading-discovered
optional definitions retain their previous complete-group policy and atomic
rollback. This avoids inflating already-working prompts merely because a
previously omitted optional group can now be expanded. The existing Section 19
receipt-question prompt is unchanged in the final database replay.

Reference scope detection also distinguishes conduct in “the following acts
are declared to be prison-offences” and “any regulation of the prison” from a
named external instrument. A bare numbered reference in an independent rule
list introduced by “may make rules consistent with this Act” can bind to a
unique same-revision target. Foreign/ambiguous scope and governing sibling
declarations still block automatic lookup; reviewed/rejected decisions and
exact source provenance remain enforced. No reference row is marked resolved
or otherwise changed in the database.

The previously blocked Section 2(b) definition now has complete required
context within the existing limits. A real `/ask` retest returned `answered`
with two freshly verified citations using one paid generation attempt. The
other three investigated cases remain blocked by unverified dependencies;
some false internal-reference failures were removed, but external-law gaps
were not guessed away.

Verification: **705 backend tests passed, with one existing OCR skip**. This
includes 44 new precision/fallback/provenance tests. A final read-only replay
covered 18 saved retrieval cases and freshly verified 451 retained source
ranges, yielding 458 proof results. All 10 previously ready cases kept identical
complete prompts, with zero ready-to-blocked regressions. The live definition
answer uses the same retained context as the final replay. The last selector
and foreign-title safeguards leave all 11 replayed reference scopes and all
2,815 published-node lookup plans unchanged. The evidence gate,
prompt builder, generation adapter and citation verifier were not changed.

The paid definition retest took about 135 seconds, including diagnostic and
concurrent-test overhead. This change does not claim a latency improvement.
Newly enabled dependency-heavy questions can carry larger prompts; all 10
tested previously ready prompts are exactly unchanged. Wider legal-form
coverage and legal-expert validation
remain necessary. Multi-reference retrieval, unresolved external sources,
latency and disabled QA audit persistence remain separate work.

Artifacts are under `tmp/step12-precision-20261024/` (work began on 24 October
and finished on 25 October). No source PDF, migration, corpus/publication write,
frontend change or environment-file edit was added.
