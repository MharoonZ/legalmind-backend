# LegalMind AI — Backend

FastAPI backend for grounded Q&A over the Contract Act, 1872 (sections 1–30).

**Sprint 1, week 1 status:** project skeleton, one shared database connection,
`POST /ask` and `GET /health`. `/ask` returns fixture JSON only (no retrieval yet).

## Contract Act corpus (Haroon)

The Sprint 1 source PDF, a 30-section draft transcription, a review workbook,
and a PostgreSQL migration are now in this repository. The PDF's SHA-256 is
`930c60f67216530fab28f37d439ad3c3ae941dc918c914f2e7c18ddbabb7bfbd`.
Sections 1–30 occupy PDF pages 11–24; inserted 19A and 30A–30C are separate
scope decisions and are excluded from the numbered corpus. The draft has
41 page-specific passages, each with a stable evidence ID. See
[`docs/corpus_review.md`](docs/corpus_review.md) for the review checklist and
source link. **All extracted text remains `needs_review`.** Importing it does
not approve it or make `/ask` use it.

To build the draft data and review workbook again:

```bash
python scripts/build_contract_act_corpus.py
node scripts/build_contract_act_review.mjs
```

The workbook builder uses the Codex `@oai/artifact-tool` runtime; the generated
workbook is already in `outputs/haroon-sprint1-20260919/`. The JSON and PDF do
not require that runtime. To create tables and import the draft into your
configured PostgreSQL database:

```bash
python scripts/ingest_contract_act_corpus.py --check-only
alembic upgrade head
python scripts/ingest_contract_act_corpus.py
```

Use a database account that can enable the `vector` extension. The migration
must be run explicitly; application startup does not create tables. Embeddings
remain null until a later, model-versioned indexing job fills them. The schema
uses PostgreSQL full-text search and `vector(3072)` storage; this small corpus
can use exact vector scans without an approximate index. The source review flag
must be checked by future retrieval code.

## Quick start

Needs Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env               # then put the real DATABASE_URL in .env
uvicorn app.main:app --reload
```

- API docs (OpenAPI): http://127.0.0.1:8000/docs
- Run tests: `pytest`

## Routes

| Method | Path      | What it does                                            |
| ------ | --------- | ------------------------------------------------------- |
| GET    | `/health` | App and database status                                 |
| POST   | `/ask`    | Question in, structured JSON out (fixture data for now) |

### `POST /ask`

Request:

```json
{ "question": "What makes an agreement a contract?" }
```

Response, supported question:

```json
{
  "supported": true,
  "answer": "An agreement is a contract when ...",
  "quote": "All agreements are contracts if ...",
  "section": 10,
  "page": 7,
  "evidence_id": "mock-ca1872-s10-p7"
}
```

Response, unsupported question (clear refusal, evidence fields are `null`):

```json
{
  "supported": false,
  "answer": "I can only answer questions that are supported by sections 1 to 30 ...",
  "quote": null,
  "section": null,
  "page": null,
  "evidence_id": null
}
```

The page numbers and evidence IDs in the fixtures are placeholders until
Haroon's reviewed PDF and database are ready.

### `GET /health`

```json
{ "status": "ok", "database": "connected", "app": "LegalMind AI API", "version": "0.1.0", "environment": "development" }
```

`status` is `"degraded"` when the database cannot be reached. The app keeps running.

## How the database connection works

- `app/main.py` creates **one** `Database` object at startup and closes it at shutdown.
- It holds one connection pool. Every request borrows a connection and gives it back.
- The pool checks each connection before use (`pool_pre_ping`) and replaces old ones
  (`pool_recycle`), so long-running servers do not break on stale connections.
- If the database is down at startup, it retries 3 times, then starts anyway.
- Routes that need the DB use `DbSessionDep` from `app/api/deps.py`.

## Project structure

```
backend/
├── app/
│   ├── main.py              # app setup, startup/shutdown, routers
│   ├── core/                # config (.env) and logging
│   ├── db/session.py        # shared engine and connection pool
│   ├── schemas/             # request/response models (the API contract)
│   ├── api/
│   │   ├── deps.py          # shared dependencies
│   │   └── routes/          # health.py, ask.py
│   ├── services/            # business logic (ask_service.py)
│   └── fixtures/            # mock JSON for /ask
├── tests/
├── .env.example
└── requirements.txt
```

## Week 2: plugging in real answers

Only `app/services/ask_service.py` needs to change. Call Haroon's retrieval, build the
answer from the passages, and return the same `AskResponse`. If retrieval is weak or
empty, return the unsupported response. The route, schema and frontend stay the same.

## Rules

- Never commit `.env`. It is already in `.gitignore`.
- Haroon owns the database schema. Do not create tables from this repo.
- Git flow: `abdullah` branch → Pull Request → `main`.

