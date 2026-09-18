# LegalMind AI — Backend

FastAPI backend for grounded Q&A over the Contract Act, 1872 (sections 1–30).

**Sprint 1, week 1 status:** project skeleton, one shared database connection,
`POST /ask` and `GET /health`. `/ask` returns fixture JSON only (no retrieval yet).

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
