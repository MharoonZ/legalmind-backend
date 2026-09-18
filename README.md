<<<<<<< HEAD
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
=======
﻿# LegalMind AI — Backend

Python FastAPI service for **LegalMind AI**, a Final Year Project that helps people and small businesses understand Pakistani legal documents, check contracts for risks, and generate grounded legal assistance with article-level citations.

**Related repo:** [legalmind-frontend](https://github.com/MharoonZ/legalmind-frontend)

---

## About the project

Legal help in Pakistan is often expensive and hard to access. General AI tools also hallucinate legal references because they are not grounded in verified Pakistani law.

LegalMind AI addresses this by:

- Summarizing legal documents in clear English with statute references
- Checking uploaded contracts for risky, missing, or inconsistent clauses
- Matching findings to verified Pakistani legal provisions (article / section / page)
- Supporting document generation from reviewed templates
- Connecting users with wakeels (lawyers) through the platform

The system focuses on Pakistani law (especially contract law and curated legal domains stored in the knowledge base). It uses retrieval-augmented generation (RAG) so answers stay tied to retrieved evidence instead of free-form guessing.

**Institution:** COMSATS University Islamabad (CUI), Lahore Campus  
**Degree:** BSc (Hons.) Software Engineering — Session 2023–2027  
**Specialization:** AI, NLP, Data Science, Web Development

---

## What this repository contains

This repo is the **backend / AI API** only:

| Area | Responsibility |
|------|----------------|
| REST / WebSocket API | Cases, contracts, analysis, auth enforcement, messaging |
| Knowledge base | Law PDF ingestion, OCR, section-aware chunking, embeddings |
| Hybrid retrieval | PostgreSQL full-text + pgvector, fusion, reranking |
| RAG workflows | Clarification → retrieval → drafting → verification (LangGraph) |
| Contract analysis | Clause risk, scenario testing, payment/deadline calculations |
| Background jobs | OCR, indexing, long reviews, re-checks after edits (Celery) |
| Evidence & audit | Immutable citations, contract versions, audit-friendly logs |

The user / wakeel / admin interfaces live in the **frontend** repo.

---

## Planned tech stack (backend)

| Layer | Technology |
|-------|------------|
| API | FastAPI + Pydantic |
| Database | PostgreSQL (Supabase) + SQLAlchemy + Alembic |
| Search | pgvector + PostgreSQL full-text search |
| Workflows | LangGraph |
| Jobs | Celery + Redis |
| Auth / storage | Supabase Auth + private Storage |
| Extraction | Docling + Tesseract OCR |
| Embeddings / LLMs | OpenAI embeddings + LLMs (evaluate on project corpus) |
| Reranking | BAAI/bge-reranker-v2-m3 |
| Export | docxtpl, LibreOffice (as needed) |
| Hosting (planned) | Railway (API + worker + Redis), Supabase |

**Architecture note:** One modular FastAPI backend owns business rules and AI. Next.js is the UI only. Node/Express and MongoDB from the early proposal are replaced by this stack.

---

## Core AI / product capabilities

1. **Legal issue taxonomy** — legal status, risk severity, drafting quality + evidence  
2. **Contextual query rewriting** — rewrite vague follow-ups using case facts  
3. **Legal case context memory** — structured facts in PostgreSQL (not raw chat dump)  
4. **Contextualized legal chunks** — embed Act, jurisdiction, section + short context  
5. **Law version awareness** — effective dates and amendment-aware retrieval  
6. **Missing-fact questions** — ask before concluding when critical facts are absent  
7. **Context compression** — keep relevant spans + evidence IDs before the main LLM  
8. **Contract scenario testing** — late payment, cancellation, missed deadlines, etc.  
9. **Payment & deadline calculator** — LLM extracts terms; code computes amounts/dates  
10. **Answer verification** — supported / partial / unsupported claim checks  
11. **Re-check after edits** — immutable contract versions drive targeted re-analysis  

---

## Roles

| Role | Access |
|------|--------|
| User | Upload contracts, ask questions, receive analysis, chat with wakeel |
| Wakeel | Review cases, advise clients, use AI-assisted tools |
| Admin | Manage users, wakeels, content, and system health |

Authorization is enforced in this API (tokens + case membership + storage access).

---

## Team

| Member | GitHub | Personal branch |
|--------|--------|-----------------|
| Mian Haroon Zahid (lead) | [MharoonZ](https://github.com/MharoonZ) | `haroon` |
| Abdullah Zahid | [MAbdullahZahid](https://github.com/MAbdullahZahid) | `abdullah` |
| Shimza Sajid | [ShimzaSajid](https://github.com/ShimzaSajid) | `shimza` |

**Supervisor:** Muhammad Shahid (Assistant Professor)

---

## Git workflow

1. Clone this repo and pull latest `main`
2. Check out **your** branch: `haroon` / `abdullah` / `shimza`
3. Develop and push to your branch
4. Open a **Pull Request** into `main`
5. Merge after review

Do **not** push directly to `main`.

```bash
git clone https://github.com/MharoonZ/legalmind-backend.git
cd legalmind-backend
git checkout <your-branch>
```

---

## Status

Project setup phase. API scaffolding, database schema, and RAG pipeline will be added next.

This software assists legal understanding; it is **not** a substitute for a licensed lawyer. Uploaded text is treated as evidence, never as instructions that override the verification workflow.
>>>>>>> f7dccf8ba0ab8040ecc1d445b6016ab48559ab7a
