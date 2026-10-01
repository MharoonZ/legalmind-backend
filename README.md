# Extraction review (read-only viewer)

backend/   FastAPI. Reads your Supabase tables, computes coverage + warnings, serves PDFs. Never writes to the DB.
frontend/  Plain HTML/JS/CSS (no build). PDF.js on the left, extracted data on the right.

## Run (two terminals)
Terminal 1:
    cd backend
    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    uvicorn main:app --port 8000
Terminal 2:
    cd frontend
    python -m http.server 5173
Open http://localhost:5173

Connection check: http://localhost:8000/api/health  (first call can take several seconds: Sydney pooler)

## Notes
- DATABASE_URL lives in backend/.env (git-ignored). You pasted the password in chat: rotate it in Supabase.
- Reviewer decisions are saved to backend/reviews.json, not the database.
- PDFs: law = downloaded from law_sources.source_url; contracts = read from documents.storage_path on the machine
  running the backend. If a PDF can't be found, use "Load PDF…" in the page; it is hash-checked against the DB.
- "Database tables" tab = every table, row browser, and foreign-key relationships (embeddings hidden).
- Tables inside text are detected heuristically (lines with | or wide column gaps). The schema has no table model.
