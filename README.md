# LegalMind AI — Backend

FastAPI backend for grounded Q&A over the Contract Act, 1872 (sections 1–30).

`POST /ask` runs a full retrieval-augmented pipeline: hybrid search over the
corpus, an evidence gate, a grounded OpenAI call, and citation validation
against the passages that were actually retrieved.

---

## The `/ask` pipeline

```
question
  ↓  validate (FastAPI + Pydantic)
  ↓  parse: does it name a section?
  ├─ "section 10" ──────────────→ fetch that section directly
  └─ otherwise
       ↓  embed question (sentence-transformers)
       ↓  keyword search (GIN on search_vector)  +  vector search (HNSW on embedding)
       ↓  merge with Reciprocal Rank Fusion → top 5 passages
  ↓  evidence gate: too weak → "unsupported" (the LLM is never called)
  ↓  build prompt (rules + passages + question)
  ↓  OpenAI → JSON {supported, answer, citations}
  ↓  validate citations: unknown evidence_id dropped, non-verbatim quote repaired
  ↓  no citation survives → "unsupported"
  ↓  AskResponse
```

Two gates matter as much as the model itself. The **evidence gate** means an
unanswerable question costs zero tokens and never reaches a model that might
guess. The **citation validator** means a hallucinated `evidence_id` can never
reach the user — it is dropped, and if nothing survives the answer is
downgraded to `unsupported`.

---

## Quick start

Needs Python 3.11+ and a PostgreSQL database where you can enable the `vector`
extension (Supabase works).

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env               # then fill in DATABASE_URL and the LLM_* lines
```

Then, in order:

```bash
# 1. Create the tables, resize the vector column, build the indexes
alembic upgrade head

# 2. Import the Contract Act corpus (verifies the PDF bytes and every hash)
python scripts/ingest_contract_act_corpus.py

# 3. Generate embeddings (first run downloads the model, ~90 MB)
python scripts/build_embeddings.py

# 4. Check your OpenAI key and model
python scripts/check_llm.py

# 5. Run it
uvicorn app.main:app --reload
```

For Sprint 2 contract extraction, process an uploaded version after applying
the latest migration:

```bash
alembic upgrade head
python scripts/extract_contract.py --version-id "docver:<id>"
```

To inspect clause IDs, text, tables, and page mappings without database writes:

```bash
python scripts/extract_contract.py --pdf path/to/sample-service-agreement.pdf
```

The extractor uses Docling for structure and provenance, PyMuPDF for canonical
page text, and strict code-based citation validation. See
`SPRINT2_EXTRACTION_NOTES.md` for the schema and summary-service handoff.

- API docs: http://127.0.0.1:8000/docs
- Readiness: http://127.0.0.1:8000/health/retrieval
- Tests: `pytest`

### Checking it works

`GET /health/retrieval` tells you exactly which setup step is still missing:

```json
{
  "ready": true,
  "reasons": [],
  "sources": 1, "sections": 30, "passages": 41, "embedded_passages": 41,
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "llm_provider": "openai", "llm_model": "gpt-4o-mini", "llm_configured": true
}
```

If `ready` is false, `reasons` names the fix ("No embeddings stored. Run
scripts/build_embeddings.py", "OPENAI_API_KEY is not set", and so on).

Then try a question:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What makes an agreement a contract?"}'
```

Set `LEGALMIND_DEBUG=true` in `.env` to get a `meta` object on every response
showing which strategy ran (`direct_section` / `hybrid` / `keyword_only`), the
top keyword and vector scores, and the candidate evidence IDs. That is the
fastest way to tune the two gate thresholds.

---

## Choosing your OpenAI model

Set two lines in `.env`, then run `python scripts/check_llm.py`:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Any chat model your account can use works. The client adapts to model
differences by itself: it steps the JSON mode down if a model rejects strict
schemas, switches `max_tokens` naming, and drops `temperature` for reasoning
models. Whatever the model returns, citations are verified against the
retrieved passages before anything reaches the user.

---

## Routes

| Method | Path                | What it does                                     |
| ------ | ------------------- | ------------------------------------------------ |
| GET    | `/health`           | App and database status                          |
| GET    | `/health/retrieval` | Is the pipeline ready? Corpus, embeddings, LLM   |
| POST   | `/ask`              | Question in, grounded JSON answer with citations |

### `POST /ask`

Request:

```json
{ "question": "What makes an agreement a contract?" }
```

Supported response:

```json
{
  "supported": true,
  "answer": "An agreement becomes a contract when the parties consent freely...",
  "quote": "All agreements are contracts if they are made by the free consent...",
  "section": 10,
  "page": 14,
  "evidence_id": "ev:23d0191582f0...",
  "citations": [
    {
      "evidence_id": "ev:23d0191582f0...",
      "section": 10,
      "section_title": "What agreements are contracts",
      "page": 14,
      "quote": "All agreements are contracts if they are made by the free consent...",
      "law_name": "Contract Act, 1872",
      "jurisdiction": "Pakistan",
      "source_url": "https://pakistancode.gov.pk/..."
    }
  ],
  "meta": null
}
```

Unsupported response — a clear refusal, evidence fields `null`:

```json
{
  "supported": false,
  "answer": "I can only answer questions that are supported by sections 1 to 30...",
  "quote": null, "section": null, "page": null, "evidence_id": null,
  "citations": [], "meta": null
}
```

**The Sprint 1 contract is unchanged.** `quote`, `section`, `page` and
`evidence_id` still mean exactly what they did, so the frontend keeps working.
They now always mirror `citations[0]`, and a validator enforces that. The new
`citations` array and `meta` object are additive and optional for the client.

**Status codes.** `200` with `supported: false` means the Act does not cover the
question. `503` means *our* pipeline is down (database or OpenAI) — the frontend
should offer a retry, not tell the user the law is silent. `422` is a malformed
question.

---

## Two schema decisions you should know about

**1. The embedding column was resized from `vector(3072)` to `vector(384)`
(migration `20260920_02`).** pgvector cannot build an HNSW or IVFFlat index
above 2000 dimensions, so the original width made the "HNSW on embedding" step
impossible and forced an exact scan on every search. `all-MiniLM-L6-v2` emits
384 dimensions; a vector column must match its model exactly. The migration
refuses to run if any embeddings already exist, so it cannot silently discard
stored vectors.

To use a different model, set `EMBEDDING_MODEL` and `EMBEDDING_DIM` together,
clear the old vectors, re-run `alembic upgrade head`, then re-embed. The model
name is stored next to every vector and search skips rows from any other model,
so two models can never be compared by accident.

**2. Retrieval is review-gated.** Every query joins `law_sources` and filters on
`review_status`. The corpus imports as `needs_review`, so the default
`RETRIEVAL_REVIEW_STATUSES=needs_review,approved` exists purely so development
works. **Set it to `approved` before any real user sees an answer**, and record
the approval properly:

```bash
python scripts/approve_source.py --list
python scripts/approve_source.py --source-id ca1872:<hash> --reviewer "Haroon Zahid"
```

The schema refuses to mark a source approved without a reviewer name and a
timestamp, which is why this is a deliberate command rather than something that
happens quietly during import.

---

## Tuning the evidence gate

`RETRIEVAL_MIN_KEYWORD_RANK` (0.02) and `RETRIEVAL_MIN_VECTOR_SIMILARITY` (0.30)
decide when a question is refused before the LLM is called. These defaults are
starting points, **not** measured values — they need tuning against the real
embedding model with a set of questions you know should be answered and a set
you know should not. Turn on `LEGALMIND_DEBUG=true`, run both sets, and read the
`meta.top_vector_score` values to pick the split.

The LLM is a second line of defence here: even if a weak match slips through the
gate, the prompt instructs the model to set `supported: false` when the evidence
does not answer the question, and an answer with no valid citation is downgraded
automatically.

---

## Project structure

```
app/
├── main.py                        # app setup, lifespan (pool, model, HTTP client)
├── core/                          # config (.env), logging, domain errors
├── db/                            # session.py (pool), models.py (tables)
├── corpus/                        # PDF extraction + hash-validated import
├── repositories/law_repository.py # keyword / vector / direct-section SQL
├── repositories/contract_extraction_repository.py # pages/clauses/status
├── schemas/                       # request & response models (the API contract)
├── services/
│   ├── ask_service.py             # the pipeline orchestrator
│   ├── question_parser.py         # "section 10", "s. 25", "u/s 23"
│   ├── embeddings.py              # sentence-transformers wrapper
│   ├── fusion.py                  # Reciprocal Rank Fusion
│   ├── prompt.py                  # grounding rules + evidence block
│   ├── citations.py               # citation & quote validation
│   ├── contract_extraction.py      # Docling adapter + deterministic clauses
│   ├── contract_citations.py       # strict uploaded-contract evidence checks
│   └── llm/                       # base.py (protocol), openai.py, factory.py
└── api/
    ├── deps.py                    # dependency wiring
    └── routes/                    # health.py, ask.py
scripts/                           # ingest, build_embeddings, approve_source, check_llm, check_db
tests/
```

Long-lived objects (connection pool, embedding model, HTTP client) are created
once in the lifespan and shared. Per-request objects (session, repository,
service) are built in `deps.py`. `AskService` never imports OpenAI directly — it
depends on the `LLMClient` protocol, so tests swap in a fake and a future model
swap touches one file.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| 503 "answering model is temporarily unavailable" | Run `python scripts/check_llm.py`; it prints the real reason (bad key, wrong model, no credit) |
| 503 "legal corpus is temporarily unavailable" | Database unreachable — check `DATABASE_URL` and `GET /health` |
| Everything returns `supported: false` | `GET /health/retrieval` — usually no embeddings yet, or `RETRIEVAL_REVIEW_STATUSES` excludes `needs_review` |
| Vector search returns nothing | `EMBEDDING_MODEL` differs from the model name stored in `law_passages.embedding_model` |
| `alembic upgrade head` raises "N passages already have embeddings" | Clear them (`UPDATE law_passages SET embedding = NULL, embedding_model = NULL`) and re-embed after migrating |
| Install too heavy / no model download | Set `EMBEDDINGS_ENABLED=false` for keyword-only retrieval |

---

## Rules

- Never commit `.env`. It is already in `.gitignore`.
- Haroon owns the corpus schema. Migration `20260920_02` only resizes the
  embedding column and adds its index — coordinate before changing anything else.
- Git flow: `abdullah` branch → Pull Request → `main`.
