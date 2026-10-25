# LegalMind backend

This repository implements full-PDF legal corpus ingestion and review, legal-aware chunking, BGE-M3 embeddings, validated corpus publications, and first-stage legal retrieval. Uploaded contracts remain a separate API. The earlier 30-section `/ask` prototype is retired; the new search endpoint returns evidence candidates rather than generated legal answers.

Phase C now also provides a new `POST /ask` that uses the active publication,
checks evidence, generates a structured draft and verifies its citations. See
[Shimza's implementation and validation notes](docs/SHIMZA_PHASE_C.md).

## Legal-PDF flow

1. Register an Act or Rules PDF and its law/edition metadata through the local admin API.
2. Run the extraction worker. It saves every physical page, ordered text blocks, extraction method, table regions, and warnings.
3. Inspect the original PDF against the saved page extraction.
4. Build a draft structure revision and run the structure worker. It saves provisions, notes, relationships, and exact source spans.
5. Review the persisted structure and audit findings; explicitly approve a revision when the review decision is justified.

Each PDF has a durable job and extraction record. A structure revision pins specific completed extraction IDs; reprocessing does not silently change historical revisions. Approval alone does not expose content to user search: a validated active publication must explicitly select its chunk set and revision.

## Chunking (Stage 6)

`scripts/build_legal_chunks.py` builds legal-aware chunks from approved revisions, following the boundary policy in [the Phase B handover](docs/ABDULLAH_PHASE_B_HANDOVER.md#71-legal-aware-chunks):

- One chunk per Section/Rule/Article/Schedule/Preamble by default; a long one is split only at a boundary between its direct children (e.g. between subsections), never mid-provision.
- Every chunk is prefixed with `Act title — label — heading`.
- `other` provisions (source notes, contents, running headers) and any provision explicitly marked `omitted`, `repealed`, or `deleted` are excluded, along with their descendants.
- A chunk set is pinned to `(revision_id, content_sha256, chunker_version, config)`; rerunning with the same inputs is a no-op, and only an `approved` revision can be chunked. Changing the chunker version or config produces a new, additional chunk set rather than overwriting the old one.

```powershell
.\.venv\Scripts\python scripts/build_legal_chunks.py --dry-run   # report only
.\.venv\Scripts\python scripts/build_legal_chunks.py             # build every approved revision
```

See `app/services/legal_chunking.py` (pure chunking logic) and `app/repositories/legal_chunk_repository.py` (loading/idempotent persistence into `chunk_sets`, `legal_chunks`, `legal_chunk_provisions`).

## Embeddings and publication retrieval (Stages 7 to 10)

The active profile is `bge-m3-v1` (`BAAI/bge-m3`, 1024 dimensions, 8192 tokens).
The builder preserves valid embeddings and repairs only missing, stale, or invalid
rows. Token limits are checked with the tokenizer; source text is not silently
truncated. Existing cosine HNSW and full-text GIN indexes are reused.

Stage 9 creates immutable `corpus_publications` and `publication_items` from an
explicit list of approved chunk sets. Creation reproduces chunks from their saved
source spans and audits every vector. Activation validates again and atomically
replaces the active publication. Published data is protected; new revisions,
independent chunk sets, and profiles remain possible.

Stage 10 `POST /search/legal` supports exact legal references, PostgreSQL FTS,
HNSW semantic candidates, and Reciprocal Rank Fusion. Only the complete active
publication is searched. Ambiguous references ask for clarification; missing
references do not fall back to unrelated sections. Optional follow-up context is
explicit request input. Candidate responses include legal/source metadata and
keyword, vector, and RRF ranks. A semantic failure is disclosed if results use
only lexical search.

Search is off by default. To enable it after activating a publication:

```powershell
$env:LEGAL_SEARCH_ENABLED = 'true'
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Use [the implementation document and full PowerShell runbook](docs/STAGE9_STAGE10_IMPLEMENTATION.md)
for the audit findings, safe migration, explicit publication creation/activation,
retrieval examples, tests, and evaluation. First semantic use loads the BGE-M3
weights; the optional audit calibration command can cache the weights beforehand.
Publication HTTP administration shares the existing loopback/development gate.
Existing local approvals do not certify current legal applicability.

The 23 pilot legal source PDFs under `uploads/legal/` are committed to the private `haroon` branch so teammates can render original pages locally. Their content-hash paths must remain unchanged because database records refer to them. Future uploads remain Git-ignored by default; do not commit private contracts, database backups, or credentials.

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

## Existing shimza branch compatibility

The current Phase A/B implementation and Phase C code are retained when joining
the earlier `shimza` history. The previous read-only PDF inspector is available
under `/local-admin/legal/inspection`, avoiding collisions with the current
`/local-admin/legal` routes. It uses the same development/loopback gate.

The earlier `scripts.run_legal_extraction` CLI and its worker use
`legacy_legal_extraction_repository.py`; the current `app.workers.legal_extract`
and `legal_extraction_repository.py` keep their existing implementation. This
separates the two repository interfaces. Earlier Sprint 2 documents are retained
as historical references; use the current workflow above for Phase A/B.

The optional Phase C audit migration is included but has not been applied to the
shared database by this delivery. Keep `QA_RECORD_RUNS=false` until separately
reviewing and approving that migration. No shared corpus or publication data is
changed by committing or pushing this branch.
