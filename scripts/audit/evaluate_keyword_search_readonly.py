"""Read-only lexical-search smoke evaluation over approved v2 chunk sets.

The expected titles are labels for these *queries*, not parser or PDF rules.
This prints actual top-five hits so a reviewer can judge relevance as well.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import ChunkSet, CorpusRevision, Provision
from app.db.session import Database
from app.repositories.legal_chunk_repository import LegalChunkRepository
from app.services.legal_chunking import ChunkConfig


CASES = [
    ("murderous outrages", "The Punjab Murderous Outrages Act, 1867"),
    ("consumer courts", "The Punjab Consumer Protection Act 2005"),
    ("borstal school", "The Punjab Borstal Act, 1926"),
    ("anti corruption establishment", "The Punjab Anti-Corruption Establishment Ordinance, 1961"),
    ("probational release", "The Good Conduct Prisoners' Probational Release Act, 1926"),
    ("prohibition expressing matters on walls", "The Punjab Prohibition of Expressing Matters on Walls Act, 1995"),
    ("possession of arms without licence", "The Punjab Arms Ordinance, 1965"),
    ("detention public order", "The Punjab Maintenance of Public Order Ordinance, 1960"),
    ("begging children", "The Punjab Vagrancy Ordinance, 1958"),
    ("medical officer prison", "The Prisons Act, 1894"),
    ("gambing", None),  # misspelling: lexical search is not spell correction
    ("zyxqv nonexistent legal phrase", None),
]


async def main(*, gate: bool = False) -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable")
        async with db.session() as session:
            revision_ids = (await session.execute(
                select(CorpusRevision.id).where(CorpusRevision.review_state == "approved")
            )).scalars().all()
        repo = LegalChunkRepository(db)
        output = []
        for query, expected in CASES:
            hits = await repo.keyword_search_revisions(revision_ids, query, limit=5)
            titles = [hit["instrument_title"] for hit in hits]
            output.append({"query": query, "expected": expected,
                           "top_1_correct": bool(titles) and titles[0] == expected if expected else not titles,
                           "top_5_contains_expected": expected in titles if expected else not titles,
                           "hits": [{"title": hit["instrument_title"],
                                     "edition": hit["edition_key"],
                                     "reference_path": hit["reference_path"],
                                     "rank": round(hit["rank"], 4)} for hit in hits[:3]]})
        async with db.session() as session:
            references = (await session.execute(
                select(Provision.revision_id, Provision.kind, Provision.label,
                       Provision.reference_path)
                .where(Provision.revision_id.in_(revision_ids),
                       Provision.kind.in_(["section", "subsection", "paragraph"]))
            )).all()
            sets = (await session.execute(
                select(ChunkSet.revision_id, ChunkSet.id)
                .where(ChunkSet.revision_id.in_(revision_ids),
                       ChunkSet.chunker_version == ChunkConfig().chunker_version,
                       ChunkSet.config_sha256 == ChunkConfig().config_sha256)
            )).all()
            historical_set_id = (await session.execute(
                select(ChunkSet.id).where(ChunkSet.chunker_version != ChunkConfig().chunker_version)
                .limit(1)
            )).scalar_one_or_none()
        set_by_revision = dict(sets)
        reference_checks = []
        by_revision = {}
        for rid, kind, label, path in references:
            by_revision.setdefault(rid, {}).setdefault((kind, label), []).append(path)
        for rid, labels in by_revision.items():
            pairs = [(label[:-1], label) for kind, label in labels
                     if kind == "section" and label and re.fullmatch(r"\d+[A-Za-z]", label)
                     and ("section", label[:-1]) in labels]
            if pairs:
                base, suffixed = pairs[0]
                chunk_set_id = set_by_revision[rid]
                found_base = await repo.find_by_label(chunk_set_id, base, kind="section")
                found_suffix = await repo.find_by_label(chunk_set_id, suffixed, kind="section")
                reference_checks.append({"case": "base_vs_lettered_section", "revision_id": str(rid),
                    "base": base, "suffixed": suffixed,
                    "base_count": len(found_base), "suffixed_count": len(found_suffix),
                    "distinct_and_exact": bool(found_base and found_suffix) and
                        all(hit["label"] == base for hit in found_base) and
                        all(hit["label"] == suffixed for hit in found_suffix) and
                        not {hit["id"] for hit in found_base} & {hit["id"] for hit in found_suffix}})
                break
        for rid, kind, label, path in references:
            if kind == "paragraph" and label in {"da", "db"} and rid in set_by_revision:
                found = await repo.find_by_reference_path(set_by_revision[rid], path)
                reference_checks.append({"case": "exact_nested_paragraph", "revision_id": str(rid),
                                         "label": label, "reference_path": path,
                                         "chunk_count": len(found), "has_source_refs":
                                         bool(found and found[0]["source_refs"])})
                break
        positives = [case for case in output if case["expected"]]
        negatives = [case for case in output if not case["expected"]]
        historical_rejected = (not await repo.keyword_search(historical_set_id, "law")
                               if historical_set_id else None)
        print(json.dumps({"revisions_in_scope": len(revision_ids),
                          "historical_set_rejected_by_current_lookup": historical_rejected,
                          "top1_correct": sum(case["top_1_correct"] for case in positives),
                          "top5_recall": sum(case["top_5_contains_expected"] for case in positives),
                          "positive_cases": len(positives),
                          "negative_cases_with_no_hits": sum(case["top_1_correct"] for case in negatives),
                          "negative_cases": len(negatives),
                          "cases": output, "reference_checks": reference_checks}, indent=2))
        if gate and (not all(case["top_1_correct"] for case in output[:6]) or
                     not all(case["distinct_and_exact"] if case["case"] == "base_vs_lettered_section"
                             else case["has_source_refs"] for case in reference_checks) or
                     historical_rejected is False):
            raise SystemExit(2)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true",
                        help="Fail if established title/reference/v1-isolation regressions recur.")
    asyncio.run(main(gate=parser.parse_args().gate))
