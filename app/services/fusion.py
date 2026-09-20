"""Reciprocal Rank Fusion (RRF).

Keyword rank scores and cosine similarities are not on the same scale, so they
cannot be added together. RRF ignores the raw scores and uses only each
passage's *position* in each result list:

    score(passage) = sum over retrievers of 1 / (k + rank)

A passage that both retrievers like beats a passage that only one of them
loves, which is exactly the behaviour we want for legal evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.repositories.law_repository import PassageHit

DEFAULT_RRF_K = 60


@dataclass(slots=True)
class FusedHit:
    """A passage with its merged score and where it came from."""

    passage: PassageHit
    rrf_score: float = 0.0
    retrievers: list[str] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def evidence_id(self) -> str:
        return self.passage.evidence_id

    def best_score(self, retriever: str) -> float:
        return self.scores.get(retriever, 0.0)


def reciprocal_rank_fusion(
    result_lists: Iterable[Sequence[PassageHit]],
    *,
    k: int = DEFAULT_RRF_K,
    top_k: int = 5,
) -> list[FusedHit]:
    """Merge several ranked lists into one, best first."""
    merged: dict[str, FusedHit] = {}

    for results in result_lists:
        for rank, hit in enumerate(results, start=1):
            entry = merged.get(hit.evidence_id)
            if entry is None:
                entry = FusedHit(passage=hit)
                merged[hit.evidence_id] = entry
            entry.rrf_score += 1.0 / (k + rank)
            if hit.retriever not in entry.retrievers:
                entry.retrievers.append(hit.retriever)
            # Keep the best rank/score seen per retriever.
            if rank < entry.ranks.get(hit.retriever, rank + 1):
                entry.ranks[hit.retriever] = rank
            entry.ranks.setdefault(hit.retriever, rank)
            if hit.score > entry.scores.get(hit.retriever, float("-inf")):
                entry.scores[hit.retriever] = hit.score

    ordered = sorted(
        merged.values(),
        # Ties broken by section then page so the same question always returns
        # the same passage order.
        key=lambda item: (
            -item.rrf_score,
            item.passage.section_number,
            item.passage.page_number,
        ),
    )
    return ordered[:top_k]
