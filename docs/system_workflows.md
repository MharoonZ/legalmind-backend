# LegalMind AI — System Workflow Diagrams

Technical diagrams for Sprint 1: **Contract-Law Q&A with Source Citations**  
Corpus: Contract Act, 1872 (Pakistan), sections **1–30**  
Stack: Next.js (planned UI) · FastAPI · PostgreSQL + pgvector

These diagrams reflect the **implemented backend** in `legalmind-backend` (`AskService`, `LawRepository`, citation validation, corpus ingest).

---

## 1. System context (what talks to what)

```mermaid
flowchart LR
  U[User] -->|typed English question| FE[Frontend<br/>Next.js planned]
  FE -->|POST /ask JSON| API[FastAPI<br/>app.main]
  API --> ASK[AskService]
  ASK --> DB[(PostgreSQL<br/>+ pgvector)]
  ASK --> EMB[EmbeddingService<br/>local model]
  ASK --> LLM[OpenAI LLM<br/>JSON answer]
  API -->|GET /health*| FE
  DB -.->|approved passages only| ASK
```

**Idea:** The UI never talks to the LLM or the database. FastAPI owns retrieval, grounding, and citation checks.

---

## 2. Offline corpus pipeline (what we built before Q&A works)

```mermaid
flowchart TD
  PDF[Contract Act PDF<br/>data/sources/*.pdf] --> EXTRACT[build_contract_act_corpus.py<br/>+ extractor]
  EXTRACT --> JSON[contract_act_1872_sections_1_30.json<br/>sections + passages + hashes]
  JSON --> MIG[alembic upgrade head<br/>tables: law_sources / sections / passages]
  JSON --> INGEST[ingest_contract_act_corpus.py]
  PDF --> INGEST
  INGEST --> DRAFT[(DB rows<br/>review_status = needs_review)]
  DRAFT --> APPROVE[approve_source.py<br/>human review]
  APPROVE --> LIVE[(review_status = approved)]
  LIVE --> EMBED[build_embeddings.py]
  EMBED --> READY[(passages.embedding + embedding_model<br/>search_vector already computed)]
```

**Gates that matter:**

| Step | Why it exists |
|------|----------------|
| PDF + JSON SHA checks | Corpus bytes must match the reviewed PDF |
| `needs_review` → `approved` | Extraction alone cannot go live |
| Embeddings script separate | Search never mixes two embedding models |

---

## 3. Data model (citation truth)

```mermaid
erDiagram
  LAW_SOURCES ||--o{ LAW_SECTIONS : contains
  LAW_SECTIONS ||--o{ LAW_PASSAGES : splits_into

  LAW_SOURCES {
    string id PK
    string law_name
    string jurisdiction
    string review_status
    string pdf_sha256
    string corpus_sha256
  }

  LAW_SECTIONS {
    string id PK
    int section_number
    string title
    text text
    int pdf_page_start
    int pdf_page_end
  }

  LAW_PASSAGES {
    string evidence_id PK
    int page_number
    text text
    tsvector search_vector
    vector embedding
    string embedding_model
  }
```

**Citation key = `evidence_id`** on `law_passages` (one section fragment on one PDF page).  
Every retrieval query joins sources and **filters `review_status`**.

---

## 4. Runtime component map (backend modules)

```mermaid
flowchart TB
  subgraph HTTP
    RASK[routes/ask.py<br/>POST /ask]
    RHEALTH[routes/health.py]
  end

  subgraph Pipeline
    AS[AskService]
    QP[question_parser]
    LR[LawRepository]
    FUS[fusion.rrf]
    EMB[embeddings]
    LLM[llm/openai]
    CIT[citations.validate]
    PR[prompt builder]
  end

  subgraph Store
    PG[(PostgreSQL)]
  end

  RASK --> AS
  AS --> QP
  AS --> LR
  AS --> FUS
  AS --> EMB
  AS --> PR
  AS --> LLM
  AS --> CIT
  LR --> PG
  EMB -.->|query vector| LR
```

---

## 5. End-to-end POST /ask sequence

```mermaid
sequenceDiagram
  actor User
  participant API as FastAPI /ask
  participant S as AskService
  participant P as question_parser
  participant R as LawRepository
  participant E as EmbeddingService
  participant L as OpenAI LLM
  participant C as citations

  User->>API: POST { question }
  API->>S: ask(question)
  S->>P: parse_question()
  alt only out-of-range sections named
    S-->>API: supported=false (out of range)
  else in-range section numbers named
    S->>R: fetch_sections([...])
    R-->>S: PassageHits (strategy=direct_section)
  else open question
    par embed + keyword
      S->>E: embed_query(question)
      S->>R: keyword_search (tsvector / GIN)
    end
    S->>R: vector_search (cosine / HNSW)
    S->>S: reciprocal_rank_fusion → top K
  end

  alt no hits OR evidence gate fails
    S-->>API: supported=false (no evidence)
  else evidence strong enough
    S->>L: grounded prompt + passage IDs
    L-->>S: JSON answer + raw citations
    alt model says unsupported
      S-->>API: supported=false
    else
      S->>C: validate_citations vs retrieved map
      alt no valid citation survives
        S-->>API: supported=false
      else
        S-->>API: supported=true + answer + quote/section/page
      end
    end
  end
  API-->>User: AskResponse JSON
```

---

## 6. AskService decision flowchart (refusal gates)

```mermaid
flowchart TD
  Q[Incoming question] --> PARSE[parse_question]
  PARSE --> OOR{Only out-of-range<br/>section numbers?}
  OOR -->|yes| U1[unsupported<br/>OUT_OF_RANGE_MESSAGE]
  OOR -->|no| RET[_retrieve]
  RET --> HITS{Any hits?}
  HITS -->|no| U2[unsupported<br/>NO_EVIDENCE_MESSAGE]
  HITS -->|yes| GATE{Evidence strong enough?<br/>direct_section OR<br/>keyword≥min OR vector≥min}
  GATE -->|no| U2
  GATE -->|yes| GEN[LLM generate JSON]
  GEN --> SUP{answer.supported?}
  SUP -->|no| U3[unsupported<br/>MODEL_REFUSED_MESSAGE]
  SUP -->|yes| VAL[validate_citations]
  VAL --> OK{≥1 valid citation?}
  OK -->|no| U2
  OK -->|yes| OKR[AskResponse supported=true<br/>flat fields = citations 0]
```

**Design rule from code:** an unanswerable question must **not** reach a guessing model when evidence is weak (gate before LLM). If the model cites nothing we retrieved, the answer is still dropped.

---

## 7. Hybrid retrieval detail

```mermaid
flowchart LR
  Q[Question text] --> KW[Keyword path<br/>websearch_to_tsquery<br/>ts_rank_cd on search_vector]
  Q --> VEC[Vector path<br/>embed_query → cosine<br/>embedding <=> query]
  KW --> RRF[Reciprocal Rank Fusion<br/>score = Σ 1/(k + rank)]
  VEC --> RRF
  RRF --> TOP[Top K FusedHits<br/>with evidence_id]

  Q2[Question names section N] --> DIR[fetch_sections<br/>skip hybrid]
  DIR --> TOP2[FusedHits<br/>retriever=section]
```

| Retriever | Signal | Index |
|-----------|--------|--------|
| `section` | Explicit “section 10” etc. | PK / section_number |
| `keyword` | Lexical / statute wording | GIN on `search_vector` |
| `vector` | Semantic similarity | HNSW on `embedding` (same `embedding_model` only) |

**Fallback:** if strict FTS returns empty → loose OR `to_tsquery` on content words (stopwords removed).  
**Degrade:** if embeddings fail/disabled → `strategy=keyword_only`.

---

## 8. Citation validation (anti-hallucination)

```mermaid
flowchart TD
  RAW[LLM citations<br/>evidence_id + quote] --> MAP{evidence_id in<br/>retrieved set?}
  MAP -->|no| DROP[Drop citation]
  MAP -->|yes| QUOTE[repair_quote]
  QUOTE --> EXACT{Normalised quote<br/>substring of passage?}
  EXACT -->|yes| KEEP[Keep verbatim quote]
  EXACT -->|no| LCS{Longest common<br/>run ≥ 30 chars?}
  LCS -->|yes| FIX[Use common run<br/>verbatim=false]
  LCS -->|no| FALL[Fallback: leading<br/>sentences of passage]
  KEEP --> OUT[ValidatedCitation]
  FIX --> OUT
  FALL --> OUT
  OUT --> LIMIT[Max 3 citations]
  LIMIT --> RESP[Populate AskResponse]
```

---

## 9. Response contract (frontend-facing)

```mermaid
flowchart TB
  subgraph Supported["supported: true"]
    A1[answer]
    A2[quote / section / page / evidence_id]
    A3[citations array]
    A2 -.->|must mirror| C0[citations 0]
  end

  subgraph Unsupported["supported: false"]
    B1[answer = refusal message]
    B2[quote/section/page/evidence_id = null]
    B3[citations = empty]
  end

  subgraph Errors["HTTP errors ≠ legal silence"]
    E1[422 invalid question]
    E2[503 corpus or LLM down]
  end
```

**Product rule:** HTTP **503** means “retry later”; **`supported: false`** means “this corpus cannot answer.”

---

## 10. Sprint ownership → code areas

```mermaid
flowchart LR
  H[Haroon<br/>corpus + retrieval + citations] --> H1[extractor / ingest / models]
  H --> H2[LawRepository + fusion]
  H --> H3[citations.py]
  A[Abdullah<br/>API + answer generation] --> A1[AskService + routes/ask]
  A --> A2[LLM + prompt + schemas]
  A --> A3[health + config + tests]
  S[Shimza<br/>UI + tests + proposal] --> S1[Next.js ask page]
  S --> S2[clickable citations]
  S --> S3[manual Q&A test pack]
```

---

## How to view

- **GitHub:** open this file; Mermaid renders automatically.
- **Local HTML:** open `docs/system_workflows.html` in a browser.
- **Source of truth:** `app/services/ask_service.py` docstring and pipeline code.
