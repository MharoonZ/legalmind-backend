"""Compare isolated dense spaces over the same active chunks without DB writes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
from pathlib import Path
from time import perf_counter

import numpy as np
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database
from app.embeddings.encoder import build_embedding_input
from app.embeddings.profiles import ACTIVE_PROFILE
from app.repositories.legal_search_repository import LegalSearchRepository, SCOPE
from scripts.benchmark_legal_rag import ranking_metrics

MODELS = {
    "bge": (ACTIVE_PROFILE.model_id, ACTIVE_PROFILE.max_tokens, "", ""),
    "minilm": ("sentence-transformers/all-MiniLM-L6-v2", 256, "", ""),
    "e5": ("intfloat/multilingual-e5-large", 512, "query: ", "passage: "),
}


async def active_chunks(include_vectors, vector_backup=None):
    db = Database(get_settings())
    try:
        repository = LegalSearchRepository(db)
        publication, _ = await repository.active_catalog()
        if publication is None:
            raise RuntimeError("No complete active publication.")
        async with repository.snapshot() as session:
            await session.execute(text("SET LOCAL statement_timeout = '90s'"))
            vector_field = (", md5(e.embedding::text) AS vector_digest" if vector_backup else
                            ", CAST(e.embedding AS real[]) AS vector" if include_vectors else "")
            rows = (await session.execute(text(
                "SELECT c.id, c.label, c.heading, c.chunk_text, i.canonical_title AS instrument_title"
                + vector_field + SCOPE + " ORDER BY c.id"
            ), {"publication_id": publication["id"], "instrument_id": None,
                "jurisdiction": None})).mappings().all()
        if not rows:
            raise RuntimeError("Publication changed during benchmark startup.")
        rows = [dict(row) for row in rows]
        if vector_backup:
            backup = json.loads(vector_backup.read_text(encoding="utf-8"))["legal_chunk_embeddings"]
            by_id = {row["chunk_id"]: row["embedding"] for row in backup}
            for row in rows:
                stored = by_id.get(str(row["id"]))
                if not isinstance(stored, str) or hashlib.md5(stored.encode()).hexdigest() != row["vector_digest"]:
                    raise ValueError("A backed-up embedding does not match the active database.")
                row["vector"] = json.loads(stored)
        return publication, rows
    finally:
        await db.dispose()


def token_distribution(lengths):
    ordered = sorted(lengths)
    return {"mean": round(statistics.mean(ordered), 2), "median": statistics.median(ordered),
            "p95": ordered[math.ceil(.95 * len(ordered)) - 1],
            "p99": ordered[math.ceil(.99 * len(ordered)) - 1], "max": ordered[-1]}


def passages(tokenizer, value, prefix, limit):
    """Use overlapping, checked windows only for the offline comparison."""
    n_tokens = len(tokenizer.encode(prefix + value, truncation=False))
    if n_tokens <= limit:
        return [prefix + value], n_tokens
    offsets = tokenizer(value, return_offsets_mapping=True, truncation=False,
                        add_special_tokens=False)["offset_mapping"]
    budget = limit - len(tokenizer.encode(prefix, truncation=False)) - 8
    start, result = 0, []
    while start < len(offsets):
        end = min(len(offsets), start + budget)
        candidate = prefix + value[offsets[start][0]:offsets[end - 1][1]]
        while len(tokenizer.encode(candidate, truncation=False)) > limit:
            end -= 1
            candidate = prefix + value[offsets[start][0]:offsets[end - 1][1]]
        result.append(candidate)
        if end == len(offsets):
            break
        start = max(start + 1, end - 48)
    return result, n_tokens


def run(args, publication, rows):
    from sentence_transformers import SentenceTransformer

    name, limit, query_prefix, passage_prefix = MODELS[args.model]
    started = perf_counter()
    model = SentenceTransformer(name, device=args.device, local_files_only=True, trust_remote_code=False)
    load_seconds = perf_counter() - started
    if model.max_seq_length < limit:
        raise ValueError("The cached model does not support the declared sequence limit.")
    model.max_seq_length = limit
    tokenizer = model.tokenizer

    documents, owners, lengths = [], [], []
    for index, row in enumerate(rows):
        source = build_embedding_input(row["instrument_title"], row["label"], row["heading"],
                                       row["chunk_text"], ACTIVE_PROFILE)
        variants, length = passages(tokenizer, source, passage_prefix, limit)
        documents.extend(variants)
        owners.extend([index] * len(variants))
        lengths.append(length)
    started = perf_counter()
    if args.model == "bge":
        vectors = np.asarray([row["vector"] for row in rows], dtype=np.float32)
    else:
        vectors = model.encode(documents, batch_size=args.batch_size, normalize_embeddings=True,
                               show_progress_bar=True)
    index_seconds = perf_counter() - started
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    records = []
    for case in cases:
        if not case["expected"]:
            continue
        question = query_prefix + case["question"]
        if len(tokenizer.encode(question, truncation=False)) > limit:
            raise ValueError(f"Benchmark query exceeds model window: {case['id']}")
        started = perf_counter()
        qv = model.encode([question], normalize_embeddings=True, show_progress_bar=False)[0]
        per_chunk = np.full(len(rows), -np.inf)
        np.maximum.at(per_chunk, owners, vectors @ qv)
        order = np.argsort(-per_chunk, kind="stable")[:10]
        ranked = [{"instrument_title": rows[i]["instrument_title"], "label": rows[i]["label"],
                   "score": float(per_chunk[i])} for i in order]
        records.append({"id": case["id"], "category": case["category"], "expected": case["expected"],
                        "seconds": perf_counter() - started, "ranked": ranked,
                        "metrics": ranking_metrics(ranked, case["expected"])})
    conceptual = [row for row in records if row["category"] not in {"exact", "multi", "multilingual"}]
    report = {
        "model": name, "dimension": vectors.shape[1], "token_limit": limit,
        "publication": publication["name"], "chunks": len(rows), "indexed_windows": len(documents),
        "parameter_mib": round(sum(p.numel() * p.element_size() for p in model.parameters()) / 2**20, 1),
        "device": str(model.device), "load_seconds": round(load_seconds, 3),
        "index_seconds": round(index_seconds, 3), "used_stored_document_vectors": args.model == "bge",
        "token_lengths": token_distribution(lengths), "inputs_over_limit": sum(n > limit for n in lengths),
        "query_seconds": token_distribution([row["seconds"] for row in records]),
        "metrics": {key: statistics.mean(row["metrics"][key] for row in records) for key in records[0]["metrics"]},
        "conceptual_metrics": {key: statistics.mean(row["metrics"][key] for row in conceptual)
                               for key in conceptual[0]["metrics"]},
        "cases": records,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path("data/rag_regression_cases.json"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--vector-backup", type=Path,
                        help="Use a local retrieval backup only after matching every vector against the DB")
    arguments = parser.parse_args()
    if arguments.vector_backup and arguments.model != "bge":
        parser.error("--vector-backup is only for the published BGE profile")
    run(arguments, *asyncio.run(active_chunks(arguments.model == "bge", arguments.vector_backup)))
