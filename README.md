# LegalMind backend

This repository implements full-PDF legal corpus ingestion and review, plus a separate uploaded-contract API. The earlier 30-section Contract Act search and `/ask` prototype are retired. The new corpus does not yet have chunking, embeddings, indexing, or a question-answering endpoint.

## Legal-PDF flow

1. Register an Act or Rules PDF and its law/edition metadata through the local admin API.
2. Run the extraction worker. It saves every physical page, ordered text blocks, extraction method, table regions, and warnings.
3. Inspect the original PDF against the saved page extraction.
4. Build a draft structure revision and run the structure worker. It saves provisions, notes, relationships, and exact source spans.
5. Review the persisted structure and audit findings; explicitly approve a revision when the review decision is justified.

Each PDF has a durable job and extraction record. A structure revision pins specific completed extraction IDs; reprocessing does not silently change historical revisions. Approved revisions are Phase B inputs, not an already searchable corpus.

The local admin UI is at `http://localhost:3000/admin/ingest` in the sibling frontend repository. It is development-only and has no production login. Do not expose it publicly.

## Local setup

Use Python 3.12+ and PostgreSQL. In PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Fill DATABASE_URL in the uncommitted .env file.
.\.venv\Scripts\python -m alembic upgrade head
```

Enable `LEGAL_INGEST_ENABLED=true` only for loopback development. Run the API and the two workers in separate terminals:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
.\.venv\Scripts\python -m app.workers.legal_extract --watch
.\.venv\Scripts\python -m app.workers.legal_structure --watch
```

A new upload queues extraction; it remains queued unless the extraction worker runs. Structure generation and approval are separate steps. The `/contracts` endpoint is a different workflow and must not be treated as the statutory corpus.

## Checks

```powershell
.\.venv\Scripts\python -m pytest -q
```

See [the Phase B handover](docs/ABDULLAH_PHASE_B_HANDOVER.md) for the approved-revision contract and [the corpus audit](docs/CORPUS_WIDE_LEGAL_PDF_AUDIT_2026-10-09_V11.md) for recorded limitations. Neither test completion nor approval alone proves legal accuracy.

## Migration history

Alembic retains historical migrations that originally created the 30-section `law_sources`, `law_sections`, and `law_passages` tables. Migration `20261009_12` drops those tables. The historical migration files and their pgvector dependency remain so databases can traverse the migration chain; they are not active search code.
