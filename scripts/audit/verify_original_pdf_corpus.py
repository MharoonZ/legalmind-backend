"""Compare active extracted pages, chunk lineage and recorded citations to originals.

All database operations are read-only. This command never reprocesses or approves
source material. Original PDFs are opened for reading only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from time import perf_counter

import fitz
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database
from app.embeddings.encoder import build_embedding_input, check_token_lengths, get_tokenizer, input_sha256
from app.embeddings.profiles import ACTIVE_PROFILE
from app.services.legal_extraction import ExtractionConfig, extract_page, resolve_source_pdf, verify_source_pdf


async def read_snapshot(db):
    async with db.session() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '60s'"))
        sources = (await session.execute(text("""
            SELECT DISTINCT f.id, f.original_filename, f.object_key, f.sha256, f.byte_size, f.page_count,
                   rs.extraction_id, i.canonical_title, pi.revision_id, pub.id AS publication_id
            FROM corpus_publications pub JOIN publication_items pi ON pi.publication_id=pub.id
            JOIN revision_sources rs ON rs.revision_id=pi.revision_id
            JOIN legal_files f ON f.id=rs.file_id
            JOIN legal_instruments i ON i.id=pi.instrument_id
            WHERE pub.status='active' ORDER BY f.original_filename
        """))).mappings().all()
        pages = (await session.execute(text("""
            SELECT p.id,p.extraction_id,p.page_index,p.raw_text,p.normalized_text,
                   p.extraction_method,p.outcome,p.warnings
            FROM file_pages p WHERE p.extraction_id IN (
                SELECT rs.extraction_id FROM revision_sources rs
                JOIN publication_items pi ON pi.revision_id=rs.revision_id
                JOIN corpus_publications pub ON pub.id=pi.publication_id WHERE pub.status='active')
            ORDER BY p.extraction_id,p.page_index
        """))).mappings().all()
        blocks = (await session.execute(text("""
            SELECT b.id,b.page_id,b.block_ordinal,b.block_kind,b.raw_text,b.normalized_text
            FROM file_blocks b WHERE b.extraction_id IN (
                SELECT rs.extraction_id FROM revision_sources rs
                JOIN publication_items pi ON pi.revision_id=rs.revision_id
                JOIN corpus_publications pub ON pub.id=pi.publication_id WHERE pub.status='active')
            ORDER BY b.page_id,b.block_ordinal
        """))).mappings().all()
        chunks = (await session.execute(text("""
            SELECT c.id,c.chunk_text,c.label,c.heading,c.revision_id,c.root_provision_id,
                   c.sequence_in_root,c.root_total_chunks,i.canonical_title,
                   e.input_sha256,e.revision_id AS embedding_revision,
                   ep.name AS profile_name,vector_dims(e.embedding) AS dimension,
                   -(e.embedding <#> e.embedding) AS squared_norm
            FROM corpus_publications pub JOIN publication_items pi ON pi.publication_id=pub.id
            JOIN legal_chunks c ON c.chunk_set_id=pi.chunk_set_id AND c.revision_id=pi.revision_id
            JOIN legal_instruments i ON i.id=pi.instrument_id
            LEFT JOIN legal_chunk_embeddings e ON e.chunk_id=c.id AND e.profile_id=pub.profile_id
            LEFT JOIN embedding_profiles ep ON ep.id=e.profile_id
            WHERE pub.status='active' ORDER BY pi.instrument_id,c.ordinal
        """))).mappings().all()
        lineage = dict((await session.execute(text("""
            SELECT count(*) AS fragment_links,
              count(*) FILTER (WHERE ps.id IS NULL OR b.id IS NULL OR ps.mapping_quality<>'exact'
                OR ps.revision_id<>c.revision_id OR p.revision_id<>c.revision_id
                OR substring(c.chunk_text FROM l.chunk_char_start+1 FOR l.chunk_char_end-l.chunk_char_start)
                   IS DISTINCT FROM substring(p.own_text FROM l.provision_char_start+1 FOR l.provision_char_end-l.provision_char_start)
                OR substring(p.own_text FROM ps.provision_char_start+1 FOR ps.provision_char_end-ps.provision_char_start)
                   IS DISTINCT FROM substring(b.normalized_text FROM ps.block_char_start+1 FOR ps.block_char_end-ps.block_char_start)
              ) AS mismatched_links
            FROM corpus_publications pub JOIN publication_items pi ON pi.publication_id=pub.id
            JOIN legal_chunks c ON c.chunk_set_id=pi.chunk_set_id AND c.revision_id=pi.revision_id
            JOIN legal_chunk_provisions l ON l.chunk_id=c.id
            LEFT JOIN provisions p ON p.id=l.provision_id
            LEFT JOIN provision_spans ps ON ps.id=l.source_span_id
            LEFT JOIN file_blocks b ON b.id=ps.block_id AND b.page_id=ps.page_id
            WHERE pub.status='active'
        """))).mappings().one())
    return [dict(r) for r in sources], [dict(r) for r in pages], [dict(r) for r in blocks], [dict(r) for r in chunks], lineage


def compare_originals(sources, pages, blocks, upload_root, config, citations):
    by_extraction, by_page = defaultdict(list), defaultdict(list)
    for page in pages:
        by_extraction[page["extraction_id"]].append(page)
    for block in blocks:
        by_page[block["page_id"]].append(block)
    files, citation_checks = [], []
    for source in sources:
        item = {key: str(source[key]) if key.endswith("id") else source[key] for key in source}
        counts, differences = Counter(), []
        try:
            path = resolve_source_pdf(str(upload_root), source["object_key"], source["sha256"])
            verify_source_pdf(path, source["sha256"])
            with fitz.open(path) as document:
                if document.page_count != source["page_count"] or path.stat().st_size != source["byte_size"]:
                    raise ValueError("Original PDF metadata mismatch")
                stored = {p["page_index"]: p for p in by_extraction[source["extraction_id"]]}
                item["missing_stored_pages"] = sorted(set(range(1,document.page_count+1))-stored.keys())
                item["extra_stored_pages"] = sorted(stored.keys()-set(range(1,document.page_count+1)))
                for index in range(document.page_count):
                    fresh = extract_page(document[index], index+1, config)
                    counts["pages"] += 1
                    counts["fresh_method_"+fresh.extraction_method] += 1
                    for warning in fresh.warnings:
                        counts["fresh_warning_"+warning["code"]] += 1
                    previous = stored.get(index+1)
                    if previous is not None:
                        counts["raw_page_matches"] += previous["raw_text"] == fresh.raw_text
                        counts["normalized_page_matches"] += previous["normalized_text"] == fresh.normalized_text
                        if previous["normalized_text"] != fresh.normalized_text:
                            differences.append({"page":index+1,"raw_equal":previous["raw_text"]==fresh.raw_text,
                                "normalized_similarity":round(SequenceMatcher(None, previous["normalized_text"],fresh.normalized_text,autojunk=False).ratio(),6),
                                "stored_warning_codes":[w.get("code") for w in previous["warnings"]]})
                        fresh_blocks = {b.ordinal:b for b in fresh.blocks}
                        for block in by_page[previous["id"]]:
                            current = fresh_blocks.get(block["block_ordinal"])
                            counts["stored_blocks"] += 1
                            counts["raw_block_matches"] += current is not None and current.raw_text == block["raw_text"]
                            counts["normalized_block_matches"] += current is not None and current.normalized_text == block["normalized_text"]
                    normalized = " ".join(fresh.normalized_text.split())
                    for citation in citations:
                        if citation["file_sha256"]==source["sha256"] and citation["page_index"]==index+1:
                            citation_checks.append({key:citation[key] for key in ("filename","page_index","reference_path","quote")}
                                                  | {"matches_original_page":" ".join(citation["quote"].split()) in normalized})
            item["original_sha256_verified"] = True
        except Exception as exc:
            item["error"] = type(exc).__name__
        item["counts"],item["page_differences"] = dict(counts),differences
        files.append(item)
    return files,citation_checks


def check_embeddings(chunks):
    failures = []
    inputs = [build_embedding_input(c["canonical_title"],c["label"],c["heading"],c["chunk_text"],ACTIVE_PROFILE) for c in chunks]
    lengths = check_token_lengths(inputs,get_tokenizer(ACTIVE_PROFILE),ACTIVE_PROFILE.max_tokens) if inputs else []
    groups = defaultdict(list)
    for chunk, assembled in zip(chunks,inputs):
        groups[(chunk["revision_id"],chunk["root_provision_id"])].append(chunk)
        if (chunk["input_sha256"]!=input_sha256(assembled) or chunk["embedding_revision"]!=chunk["revision_id"]
                or chunk["profile_name"]!=ACTIVE_PROFILE.name or chunk["dimension"]!=ACTIVE_PROFILE.dimension
                or chunk["squared_norm"] is None or abs(chunk["squared_norm"]-1)>.002):
            failures.append(str(chunk["id"]))
    bad_sequences = sum(sorted(c["sequence_in_root"] for c in rows)!=list(range(1,len(rows)+1))
                        or any(c["root_total_chunks"]!=len(rows) for c in rows) for rows in groups.values())
    return {"chunks":len(chunks),"maximum_input_tokens":max(lengths,default=0),
            "profile":ACTIVE_PROFILE.name,"dimension":ACTIVE_PROFILE.dimension,
            "input_or_vector_identity_failures":failures,"bad_root_sequences":bad_sequences}


def load_citations(paths):
    unique = {}
    for path in paths:
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            body = case.get("body") or case.get("answer")
            if isinstance(body,dict):
                for citation in body.get("citations",[]):
                    unique[(citation["file_sha256"],citation["page_index"],citation["quote"])] = citation
    return list(unique.values())


async def verify(output, response_paths):
    started = perf_counter()
    settings = get_settings()
    db = Database(settings)
    try:
        sources,pages,blocks,chunks,lineage = await read_snapshot(db)
    finally:
        await db.dispose()
    config = ExtractionConfig(tessdata_dir=settings.legal_ocr_tessdata_dir,
                              languages=settings.legal_ocr_languages,dpi=settings.legal_ocr_dpi)
    citations = load_citations(response_paths)
    files,proofs = await asyncio.to_thread(compare_originals,sources,pages,blocks,Path(settings.legal_upload_dir),config,citations)
    embeddings = await asyncio.to_thread(check_embeddings,chunks)
    counts = Counter()
    for item in files:
        counts.update(item["counts"])
    result = {"files":files,"page_totals":dict(counts),"lineage":lineage,"embeddings":embeddings,
              "citation_checks":proofs,"citation_inputs":len(citations),
              "summary":{"originals":len(files),"original_errors":sum("error" in f for f in files),
                         "pages":counts["pages"],"raw_page_mismatches":counts["pages"]-counts["raw_page_matches"],
                         "normalized_page_mismatches":counts["pages"]-counts["normalized_page_matches"],
                         "raw_block_mismatches":counts["stored_blocks"]-counts["raw_block_matches"],
                         "citation_checks":len(proofs),"citation_mismatches":sum(not p["matches_original_page"] for p in proofs),
                         "unmatched_citation_inputs":len(citations)-len(proofs),"seconds":round(perf_counter()-started,3)},
              "scope":"Fresh extraction from original byte-verified PDFs, compared to active stored sources. Differences require inspection; no data or approvals were changed."}
    output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps(result["summary"]))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--responses",type=Path,action="append",default=[])
    args=parser.parse_args()
    try:
        asyncio.run(verify(args.output,args.responses))
    except Exception as exc:
        raise SystemExit(f"Original PDF verification failed ({type(exc).__name__}).") from None
