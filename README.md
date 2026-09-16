# LegalMind AI — Backend

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
