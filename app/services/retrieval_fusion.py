"""Pure candidate relevance checks and reciprocal rank fusion."""


def filter_keyword_candidates(rows, *, semantic_available, min_similarity):
    """Weak OR matches need semantic support; lexical-only fallback needs coverage."""
    accepted = []
    for row in rows:
        if row["keyword_strict_match"]:
            accepted.append(row)
        elif semantic_available:
            if row["lexical_vector_score"] is not None and row["lexical_vector_score"] >= min_similarity:
                accepted.append(row)
        elif (row["keyword_matched_terms"] >= 2 and
              row["keyword_matched_terms"] / max(row["keyword_total_terms"], 1) >= .6):
            accepted.append(row)
    return accepted


def reciprocal_rank_fusion(keyword, vector, *, k=60):
    if k < 1:
        raise ValueError("RRF k must be positive")
    merged = {}
    for kind, rows in (("keyword", keyword), ("vector", vector)):
        seen = set()
        for row in rows:
            identity = row["chunk_id"]
            if identity in seen:
                continue
            seen.add(identity)
            rank = len(seen)
            item = merged.setdefault(identity, {**row, "keyword_rank": None, "vector_rank": None,
                                               "rrf_score": 0.0})
            item[f"{kind}_rank"] = rank
            item[f"{kind}_score"] = float(row[f"{kind}_score"])
            item["rrf_score"] += 1 / (k + rank)
    result = sorted(merged.values(), key=lambda r: (-r["rrf_score"],
        -r.get("vector_score", -1), -r.get("keyword_score", 0), str(r["chunk_id"])))
    return [{**row, "rank": rank, "rrf_rank": rank} for rank, row in enumerate(result, 1)]
