"""Read-only extraction review backend.

Reads your existing tables (law_* and documents/document_versions/contract_*),
computes coverage + warnings, serves the PDF, and stores reviewer decisions in a
local reviews.json file. It never writes to the database.
"""
import hashlib, json, os, re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg, httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()
BASE = Path(__file__).parent
REVIEWS = BASE / "reviews.json"
CACHE = BASE / "pdf_cache"
CACHE.mkdir(exist_ok=True)
HIDE_COLS = {"embedding", "search_vector"}  # huge/unreadable, hidden in the DB browser
pool = None


@asynccontextmanager
async def lifespan(app):
    global pool
    # statement_cache_size=0 -> required for the Supabase pooler (pgbouncer style)
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=1, max_size=3,
        timeout=60, command_timeout=60, statement_cache_size=0, ssl="require",
    )
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def q(sql, *args):
    """Every query runs inside a READ ONLY transaction."""
    async with pool.acquire() as c:
        async with c.transaction(readonly=True):
            return [dict(r) for r in await c.fetch(sql, *args)]


sha = lambda s: hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def new_warnings():
    W = []
    def w(level, where, msg, page=None, ref=None):
        W.append({"level": level, "where": where, "msg": msg, "page": page, "ref": ref})
    return W, w


def dup_check(w, groups, label):
    for h, where in groups.items():
        if len(where) > 1:
            w("warn", ", ".join(where[:4]), f"Duplicate {label} content (same hash) in {len(where)} places")


def coverage(n, pm, expected_items, found_items):
    empty = [p["page"] for p in pm.values() if p["chars"] == 0]
    unmapped = [p["page"] for p in pm.values() if p["items"] == 0]
    return {"expected_pages": n, "pages_with_text": n - len(empty), "empty_pages": empty,
            "unmapped_pages": unmapped, "expected_items": expected_items,
            "found_items": found_items, "page_map": list(pm.values())}


@app.get("/api/health")
async def health():
    return {"db": (await q("select 1 as ok"))[0]["ok"] == 1}


@app.get("/api/items")
async def items():
    law = await q("select id, law_name, jurisdiction, review_status, pdf_page_count from law_sources order by created_at desc")
    con = await q("""select v.id, d.original_filename, v.version_number, v.status, d.page_count
                     from document_versions v join documents d on d.id = v.document_id
                     order by v.created_at desc""")
    return {"law": law, "contracts": con}


# ------------------------------------------------------------------ law corpus
@app.get("/api/law/{sid}")
async def law(sid: str):
    src = await q("select * from law_sources where id=$1", sid)
    if not src:
        raise HTTPException(404, "law source not found")
    src = src[0]; n = src["pdf_page_count"]
    secs = await q("select * from law_sections where source_id=$1 order by section_number", sid)
    pas = await q("""select p.evidence_id, p.section_id, p.page_number, p.text, p.text_sha256,
                            (p.embedding is not null) as has_embedding
                     from law_passages p join law_sections s on s.id = p.section_id
                     where s.source_id=$1 order by p.section_id, p.page_number""", sid)
    W, w = new_warnings()
    by = {s["id"]: dict(s, passages=[]) for s in secs}
    for p in pas:
        by[p["section_id"]]["passages"].append(p)
    nums = {s["section_number"] for s in secs}
    top = max(nums, default=0)
    if not secs:
        w("error", "Source", "No sections were extracted for this source")
    for i in range(1, top + 1):
        if i not in nums:
            w("error", f"§{i}", f"Section number {i} is missing (gap in 1..{top})")
    pm = {pg: {"page": pg, "chars": 0, "items": 0} for pg in range(1, n + 1)}
    sec_hash, pas_hash, out = {}, {}, []
    for s in by.values():
        wh = f"§{s['section_number']}"; a, b, ref = s["pdf_page_start"], s["pdf_page_end"], s["id"]
        if not s["text"].strip(): w("error", wh, "Section text is empty", a, ref)
        else: sec_hash.setdefault(s["text_sha256"], []).append(wh)
        if b > n: w("error", wh, f"Page range {a}-{b} goes past the PDF page count ({n})", a, ref)
        if sha(s["text"]) != s["text_sha256"]:
            w("warn", wh, "Stored text hash differs from recomputed hash (text changed, or normalised before hashing)", a, ref)
        if not s["passages"]: w("warn", wh, "No passages / evidence IDs for this section", a, ref)
        for pg in range(a, min(b, n) + 1): pm[pg]["items"] += 1
        for p in s["passages"]:
            pg = p["page_number"]; pw = f"{wh} p.{pg}"
            if not p["text"].strip(): w("error", pw, "Passage text is empty", pg, ref)
            else: pas_hash.setdefault(p["text_sha256"], []).append(p["evidence_id"])
            if pg > n or not a <= pg <= b:
                w("error", pw, f"Passage page {pg} is outside the section range {a}-{b} or the PDF ({n} pages)", pg, ref)
            elif pg in pm: pm[pg]["chars"] += len(p["text"])
            if sha(p["text"]) != p["text_sha256"]:
                w("warn", pw, "Passage hash differs from recomputed hash", pg, ref)
        out.append({"id": ref, "title": f"§{s['section_number']} {s['title']}", "sub": f"pages {a}-{b} · {len(s['passages'])} passages",
                    "p1": a, "p2": b, "text": s["text"], "sha": s["text_sha256"], "passages": s["passages"]})
    dup_check(w, sec_hash, "section"); dup_check(w, pas_hash, "passage")
    cov = coverage(n, pm, top, len(secs))
    if cov["empty_pages"]:
        w("warn", "Coverage", f"{len(cov['empty_pages'])} PDF pages have no extracted passage: {cov['empty_pages'][:15]}", cov["empty_pages"][0])
    return {"kind": "law", "meta": src, "expectedPages": n, "items": out, "warnings": W, "coverage": cov, "pagesText": None}


# ------------------------------------------------------------------- contracts
@app.get("/api/contract/{vid}")
async def contract(vid: str):
    v = await q("""select v.*, d.original_filename, d.pdf_sha256, d.page_count, d.size_bytes, d.storage_path
                   from document_versions v join documents d on d.id = v.document_id where v.id=$1""", vid)
    if not v:
        raise HTTPException(404, "document version not found")
    v = v[0]; n = v["page_count"]
    pages = await q("select page_number, text, text_sha256 from contract_pages where document_version_id=$1 order by 1", vid)
    clauses = await q("select * from contract_clauses where document_version_id=$1 order by ordinal", vid)
    spans = await q("""select clause_id, page_number, clause_char_start as s, clause_char_end as e
                       from contract_clause_spans where document_version_id=$1 order by clause_id, clause_char_start""", vid)
    W, w = new_warnings()
    if v["status"] != "extracted" and v["status"] not in ("summarizing", "summarized", "summary_failed"):
        w("warn", "Version", f"Status is '{v['status']}'" + (f": {v['error_message']}" if v["error_message"] else ""))
    ptext = {p["page_number"]: p for p in pages}
    pm = {pg: {"page": pg, "chars": len(ptext[pg]["text"].strip()) if pg in ptext else 0, "items": 0} for pg in range(1, n + 1)}
    for pg in range(1, n + 1):
        if pg not in ptext: w("error", f"Page {pg}", "No extracted page row", pg)
        elif not ptext[pg]["text"].strip(): w("warn", f"Page {pg}", "Page text is empty (scanned image?)", pg)
        elif sha(ptext[pg]["text"]) != ptext[pg]["text_sha256"]: w("warn", f"Page {pg}", "Page hash differs from recomputed hash", pg)
    for pg in ptext:
        if pg > n: w("error", f"Page {pg}", f"Extracted page {pg} is beyond the PDF page count ({n})", pg)
    sp = {}
    for s in spans: sp.setdefault(s["clause_id"], []).append(s)
    ords = {c["ordinal"] for c in clauses}
    top = max(ords, default=0)
    if not clauses: w("error", "Clauses", "No clauses were extracted")
    for i in range(1, top + 1):
        if i not in ords: w("error", f"Clause #{i}", f"Clause ordinal {i} is missing (gap in 1..{top})")
    hashes, out = {}, []
    for c in clauses:
        name = c["label"] or f"#{c['ordinal']}"; wh = f"Clause {name}"; a, b, ref = c["page_start"], c["page_end"], c["id"]
        L = len(c["text"]); cs = sp.get(ref, [])
        if not c["text"].strip(): w("error", wh, "Clause text is empty", a, ref)
        else: hashes.setdefault(c["text_sha256"], []).append(wh)
        if b > n: w("error", wh, f"Pages {a}-{b} go past the PDF page count ({n})", a, ref)
        if sha(c["text"]) != c["text_sha256"]: w("warn", wh, "Clause hash differs from recomputed hash", a, ref)
        if not cs: w("warn", wh, "Clause has no page-mapping spans", a, ref)
        cur = gap = 0
        for s in cs:
            if s["e"] > L: w("error", wh, f"Span {s['s']}-{s['e']} runs past the clause text length ({L})", s["page_number"], ref)
            if not a <= s["page_number"] <= b: w("error", wh, f"Span maps to page {s['page_number']}, outside the clause range {a}-{b}", s["page_number"], ref)
            if s["s"] > cur: gap += s["s"] - cur
            cur = max(cur, s["e"])
        if cs: gap += max(0, L - cur)
        if cs and gap > 5: w("warn", wh, f"{gap} characters of this clause are not mapped to any page", a, ref)
        for pg in range(a, min(b, n) + 1): pm[pg]["items"] += 1
        out.append({"id": ref, "title": f"{name} {c['heading'] or ''}".strip(), "sub": f"pages {a}-{b} · {len(cs)} spans",
                    "p1": a, "p2": b, "text": c["text"], "sha": c["text_sha256"], "spans": cs})
    dup_check(w, hashes, "clause")
    cov = coverage(n, pm, top, len(clauses))
    if cov["unmapped_pages"]:
        w("info", "Coverage", f"{len(cov['unmapped_pages'])} pages are not covered by any clause: {cov['unmapped_pages'][:15]}", cov["unmapped_pages"][0])
    meta = {k: v[k] for k in ("original_filename", "status", "version_number", "pdf_sha256", "page_count", "extraction_engine",
                              "extraction_engine_version", "extracted_text_sha256", "extraction_completed_at", "error_message")}
    return {"kind": "contract", "meta": meta, "expectedPages": n, "items": out, "warnings": W, "coverage": cov, "pagesText": pages}


# ------------------------------------------------------------------------ PDFs
async def pdf_response(src, name):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    f = CACHE / name
    if src.startswith("http"):
        if not f.exists():
            async with httpx.AsyncClient(follow_redirects=True, timeout=90) as c:
                r = await c.get(src); r.raise_for_status()
            f.write_bytes(r.content)
        return FileResponse(f, media_type="application/pdf")
    for cand in (Path(src), BASE / src, BASE.parent / src):
        if cand.is_file(): return FileResponse(cand, media_type="application/pdf")
    raise HTTPException(404, f"PDF file not found at '{src}'. Use 'Load PDF' in the page.")


@app.get("/api/pdf/law/{sid}")
async def pdf_law(sid: str):
    r = await q("select source_url from law_sources where id=$1", sid)
    if not r: raise HTTPException(404, "not found")
    try: return await pdf_response(r[0]["source_url"], f"law_{sid}.pdf")
    except httpx.HTTPError as e: raise HTTPException(502, f"Could not download source_url: {e}")


@app.get("/api/pdf/contract/{vid}")
async def pdf_contract(vid: str):
    r = await q("select d.storage_path from document_versions v join documents d on d.id=v.document_id where v.id=$1", vid)
    if not r: raise HTTPException(404, "not found")
    return await pdf_response(r[0]["storage_path"], f"doc_{vid}.pdf")


# --------------------------------------------------- reviews (local JSON file)
class Review(BaseModel):
    key: str
    status: str
    note: str = ""
    reviewer: str = ""


def load_reviews():
    return json.loads(REVIEWS.read_text("utf-8")) if REVIEWS.exists() else {}


@app.get("/api/reviews")
async def get_reviews():
    return load_reviews()


@app.post("/api/reviews")
async def save_review(r: Review):
    if r.status not in ("needs_review", "verified", "problem"):
        raise HTTPException(400, "bad status")
    data = load_reviews()
    data[r.key] = {**r.model_dump(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    REVIEWS.write_text(json.dumps(data, indent=2), "utf-8")
    return data[r.key]


# ------------------------------------------------- raw table + relationship view
@app.get("/api/db/overview")
async def db_overview():
    tabs = await q("select table_name from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1")
    out = []
    for t in tabs:
        name = t["table_name"]
        cols = await q("select column_name, data_type from information_schema.columns where table_schema='public' and table_name=$1 order by ordinal_position", name)
        cnt = (await q(f'select count(*) as c from "{name}"'))[0]["c"]
        out.append({"name": name, "rows": cnt, "columns": cols})
    fks = await q("""select conrelid::regclass::text as from_table, confrelid::regclass::text as to_table,
                            pg_get_constraintdef(oid) as definition
                     from pg_constraint where contype='f' and connamespace='public'::regnamespace order by 1""")
    return {"tables": out, "relations": fks}


@app.get("/api/db/table/{name}")
async def db_table(name: str, limit: int = 25, offset: int = 0):
    cols = await q("select column_name from information_schema.columns where table_schema='public' and table_name=$1 order by ordinal_position", name)
    if not cols: raise HTTPException(404, "unknown table")
    names = [c["column_name"] for c in cols if c["column_name"] not in HIDE_COLS]
    sel = ", ".join(f'"{c}"' for c in names)
    rows = await q(f'select {sel} from "{name}" limit $1 offset $2', min(limit, 200), offset)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, str) and len(v) > 600: r[k] = v[:600] + f"… [{len(v)} chars]"
            elif not isinstance(v, (str, int, float, bool, type(None))): r[k] = str(v)
    return {"columns": names, "rows": rows}
