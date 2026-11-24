# LegalMind backend

LegalMind answers questions about a collection of laws. It ingests the official
PDFs, keeps the structure of each law (sections, sub-sections, schedules),
publishes a reviewed snapshot of the collection, and answers questions from
that snapshot only. Every answer cites the provision and the PDF page it
quotes, and each quotation is checked against the published text before it is
returned.

The API is a FastAPI application backed by PostgreSQL (with pgvector). The
answer model is Google Gemini; OpenAI can be used instead or as a failover.

## How it works

```
Phase A  Ingestion (local administration)
  upload PDF -> extract text / OCR per page -> parse legal structure -> review and approve

Phase B  Indexing
  legal-aware chunks -> keyword index -> embeddings (BAAI/bge-m3) -> immutable publication

Phase C  Question answering (POST /ask)
  understand the question     law names and section numbers, tolerant of typos; follow-ups
  retrieve                    exact provision lookup, BM25 keywords, dense vectors, rank fusion
  rerank (optional)           cross-encoder, only when the hardware is fast enough
  assemble evidence           whole provisions, definitions, cross-referenced provisions
  generate                    one structured model call
  verify citations            every quote located in the published text and its PDF page
```

The active publication is loaded into memory once and refreshed when another
publication is activated, so answering a question makes no database queries.
A fuller description, with the reasoning behind each stage, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository layout

```
app/
  api/            routes, dependencies (API key, rate limit), request middleware
  core/           settings, logging, inference queue, rate limiter
  db/             SQLAlchemy models and the async session
  retrieval/      Phase C first stage: snapshot, law resolver, reference parser, BM25, fusion
  qa/             Phase C answer stage: rerank, evidence, prompt, model client, citations
  embeddings/     embedding profiles, encoder and vector storage (Phase B)
  services/       Phase A/B logic: extraction, structure parser, chunking, audits, contracts
  repositories/   Phase A/B database access
  workers/        background jobs: page extraction and structure parsing
  schemas/        request and response models
scripts/
  pipeline/       commands that build the corpus (chunks, embeddings, publications)
  audit/          read-only inspection commands
  eval/           retrieval and end-to-end evaluation
alembic/          database migrations
data/             evaluation cases and review manifests
tests/            unit, API and (opt-in) PostgreSQL integration tests
docs/             architecture, audit report, Phase A/B references, archive
```

## Requirements

- Python 3.12 or newer
- PostgreSQL with the `vector` extension (the project database is Supabase;
  the test suite's integration tests were run on PostgreSQL 16)
- About 5 GB of disk for the models (embedding model 2.3 GB, reranker 2.3 GB; the
  reranker is optional)
- A Gemini API key (or an OpenAI key)

No GPU is needed. On a CPU-only machine a question is answered in one to
three seconds, nearly all of it the answer model's own response time; the
server needs about 2.5 GB of memory.

## Setup

Linux and macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only hosts; skips the CUDA build
pip install -r requirements-dev.txt                                   # or requirements.txt without test tools
cp .env.example .env                                                  # then edit .env
alembic upgrade head
python -m scripts.pipeline.download_models                            # add --skip-reranker to save 2.3 GB
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt
Copy-Item .env.example .env
alembic upgrade head
python -m scripts.pipeline.download_models
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The minimum `.env` for answering questions:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/postgres
LEGAL_SEARCH_ENABLED=true
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
```

Special characters in the database password must be URL-encoded (`#` becomes
`%23`). `.env` is ignored by Git; never commit it.

`GET /health/ready` returns 200 once the publication is in memory. The models
load in the background right after start-up (`models_ready` in the same
response); the first question after a cold start waits for them.

## Configuration

Every setting is an environment variable read by `app/core/config.py`; the
common ones are listed with comments in `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` turns off `/docs` and the local administration routes |
| `LEGAL_SEARCH_ENABLED` | `false` | Enables `/ask` and `/search/legal` |
| `API_KEYS` | empty | Comma-separated keys for `/ask` and `/search/legal`; empty leaves them open |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per-client limit on those two routes |
| `CORS_ORIGINS` | `http://localhost:3000` | Frontends allowed to call the API |
| `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` / `gemini-3.1-flash-lite` | Answer model, and the one used when it is overloaded or rate limited |
| `QA_GEMINI_TIMEOUT_SECONDS` | `25` | Time allowed for one answer, retries included |
| `QA_RERANK_MODE` | `auto` | `auto` reranks only when the host can do it inside `QA_RERANK_BUDGET_MS` |
| `PUBLICATION_CACHE_DIR` | `.cache/publications` | Disk cache of the publication, so restarts skip the database transfer |
| `DB_SSL_ROOT_CERT` | empty | CA file used to verify the database server's certificate |
| `LEGAL_INGEST_ENABLED` | `false` | Local administration (ingestion, review, publications); development and loopback only |

## Building the collection

Run these from the repository root with the virtual environment active. The
ingestion routes exist only when `ENVIRONMENT=development` and
`LEGAL_INGEST_ENABLED=true`, and only answer requests from the same machine.

1. **Register PDFs.** Use the frontend's local ingestion screen, or
   `POST /local-admin/legal/uploads`.
2. **Extract pages and parse structure.** Two workers process the queued jobs:

   ```bash
   python -m app.workers.legal_extract --watch
   python -m app.workers.legal_structure --watch
   ```

   Without `--watch` each processes one job and exits. Both can be stopped at
   any time; an interrupted job is released and picked up again.
3. **Review and approve** each revision in the local review screen, or list and
   approve the latest revisions from the command line:

   ```bash
   python -m scripts.pipeline.approve_latest_legal_revisions                      # read-only report
   python -m scripts.pipeline.approve_latest_legal_revisions --apply --reason "Reviewed against the source PDFs"
   ```
4. **Chunk and embed** the approved revisions (both are safe to re-run):

   ```bash
   python -m scripts.pipeline.build_legal_chunks
   python -m scripts.pipeline.build_legal_embeddings
   ```
5. **Publish.** A publication is an immutable list of chunk sets. Nothing is
   served until one is activated, and activation is always explicit:

   ```bash
   python -m scripts.pipeline.manage_publications plan --output plan.json
   python -m scripts.pipeline.manage_publications create --manifest plan.json --name pilot-2026-11
   python -m scripts.pipeline.manage_publications activate --publication-id <id from the previous command>
   ```

   A running server notices the new publication within
   `PUBLICATION_REFRESH_SECONDS` (60 by default); no restart is needed.

Read-only inspection commands live in `scripts/audit/`; each prints its options
with `--help`. For example:

```bash
python -m scripts.audit.check_db
python -m scripts.audit.search_legal "punishment for gaming in a public place"
python -m scripts.audit.ask "What is section 3 of the Punjab Prevention of Gambling Ordinance 1978?"
```

## API

### `POST /ask`

```json
{
  "question": "What is Section 3 of Punjb Prevention of Gambling Ordinance 1978?",
  "history": [
    {"role": "user", "content": "an earlier question"},
    {"role": "assistant", "content": "the earlier answer"}
  ]
}
```

Only `question` is required. `history` (oldest first) lets follow-ups such as
"what is the penalty for that?" work; `law` restricts the question to one law.

```json
{
  "status": "answered",
  "question": "...",
  "answer": "Section 3 of the Punjab Prevention of Gambling Ordinance, 1978 ... [1]",
  "citations": [
    {
      "law": "The Punjab Prevention of Gambling Ordinance, 1978",
      "kind": "section", "label": "3", "page_index": 3,
      "quote": "shall be punishable with imprisonment for a term which shall not be less than one month ...",
      "filename": "112.pdf", "file_sha256": "...", "source_span_id": "..."
    }
  ],
  "limitations": ["This is general information from the cited legal text, not legal advice."],
  "clarification_question": null,
  "clarification_options": [],
  "missing_support": null
}
```

`[1]` in the answer refers to `citations[0]`. Quotes are the law's own wording,
taken from the published text.

| `status` | HTTP | Meaning |
|---|---|---|
| `answered` | 200 | The evidence answers the question |
| `partial` | 200 | Part of it is answered; `missing_support` says what is not |
| `clarification_needed` | 200 | The question is ambiguous; `clarification_options` lists the laws to choose from |
| `insufficient_evidence` | 200 | The collection does not cover it |
| `service_unavailable` | 503 | The answer model, the database or the server is temporarily unavailable; retry |

During a model outage, named provisions remain available. A topical question
can also return a relevant passage with page citations and status `partial`.
Its `missing_support` field explains that a generated interpretation is unavailable.
The `Server-Timing`
response header reports the time spent in each stage and `X-Request-ID`
matches the request's log lines.

### `POST /search/legal`

Returns the ranked evidence passages for a query without generating an answer.
Useful for debugging retrieval.

### Health

- `GET /health/live`: the process is running.
- `GET /health/ready`: 200 when the service can answer questions.
- `GET /health`: database, publication and model status in one response.

### Protecting a public deployment

Set `API_KEYS`; clients then send `X-API-Key: <key>` (or `Authorization: Bearer
<key>`). Requests are limited per client by `RATE_LIMIT_PER_MINUTE`, request
bodies are size-limited, and at most `QA_MAX_CONCURRENT_REQUESTS` questions run
at once; the rest wait up to `QA_QUEUE_TIMEOUT_SECONDS`.

## Testing

```bash
pytest                      # unit and API tests; no database, model or network needed
ruff check .                # lint
```

Integration tests read a real database (nothing is written). They are skipped
unless a URL is given, and work with any published collection:

```bash
LEGALMIND_TEST_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/postgres pytest tests/test_integration_postgres.py
```

End-to-end evaluation asks the labelled questions in `data/ask_evaluation.json`
(misspelt law names, abbreviations, ranges, paraphrases, Roman Urdu,
follow-ups, laws that are not in the collection) and checks the status and the
cited provision. It uses the configured database and answer model:

```bash
python -m scripts.eval.evaluate_retrieval                                   # retrieval only, no model calls
python -m scripts.eval.evaluate_ask                                         # in process
python -m scripts.eval.evaluate_ask --url http://127.0.0.1:8000 --output report.json
python -m scripts.eval.evaluate_ask --pause 5                               # for a key with a low per-minute quota
```

The cases refer to the pilot collection of 23 Punjab laws. For a different
collection, edit the file: each case names a question, the allowed statuses and
the provision that should be cited.

## Deployment

- Run the API with a process manager, for example
  `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`. Each worker
  process loads its own copy of the models (about 2.5 GB without the
  reranker); add workers only if memory allows, or run several single-worker
  containers behind a load balancer.
- Set `ENVIRONMENT=production`, `API_KEYS`, the real `CORS_ORIGINS`, and
  `LOG_FORMAT=json`. Leave `LEGAL_INGEST_ENABLED=false`: ingestion and review
  are local administration tools without a login.
- Run `alembic upgrade head` before starting a new version, and
  `python -m scripts.pipeline.download_models` in the image build so start-up
  does not download anything.
- Point the load balancer's health check at `/health/ready`.
- Set `DB_SSL_ROOT_CERT` to the database provider's CA certificate so the
  server's certificate is verified.
- A `Dockerfile` is included: `docker build -t legalmind-backend .` then
  `docker run --env-file .env -p 8000:8000 legalmind-backend`. Docker reads an
  env file literally, so write `NAME=value` there with no spaces around `=`
  and no quotes. The image contains the models; build with
  `--build-arg SKIP_RERANKER=true` to leave out the 2.3 GB reranker.

The rate limiter and the answer-model cool-down state are per process. With
several processes each applies its own limit.

## Troubleshooting

- **`/ask` returns 503 "no active publication".** Nothing has been activated in
  this database; see "Building the collection".
- **Answers take 10 seconds or more.** Look for `llm_attempt_failed` in the
  log. `status=503` means the model is overloaded and `status=429` means the
  key's rate limit was reached (`retry_after` shows how long the provider asked
  callers to wait). The server retries and switches to the fallback model
  within the time limit; a higher-quota key or an `OPENAI_API_KEY` failover
  removes the delay.
- **The first question after start-up is slow.** The models are still loading;
  wait for `models_ready` in `/health/ready`.
- **A law is not found.** `python -m scripts.audit.search_legal "<its name>"`
  shows what the name resolves to. Names are matched with tolerance for
  typos, but the law must be in the active publication.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how each stage works and why.
- [docs/AUDIT_2026-11.md](docs/AUDIT_2026-11.md): the November 2026 audit: root
  causes found, what changed, measurements and known limits.
- [docs/ABDULLAH_PHASE_B_HANDOVER.md](docs/ABDULLAH_PHASE_B_HANDOVER.md) and
  [docs/ABDULLAH_CHUNK_KEYWORD_HANDOVER_2026-10-13.md](docs/ABDULLAH_CHUNK_KEYWORD_HANDOVER_2026-10-13.md):
  chunking and keyword index reference (Phase B).
- [docs/HAROON_LEGAL_CORPUS_SCHEMA.md](docs/HAROON_LEGAL_CORPUS_SCHEMA.md): corpus tables (Phase A).
- [docs/archive/](docs/archive/): earlier handovers, reviews and reports, kept
  for history. They describe code that has since been replaced.
