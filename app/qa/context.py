"""Stage 12: assemble the evidence the answer model may use.

Each selected provision is included whole (every part of a split section), in
rank order, until the budget is reached. Definitions and same-law provisions
that the selected text refers to are added when there is room. Assembly never
fails a question: whatever does not fit is simply left out.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.retrieval.query import QueryAnalysis
from app.retrieval.references import parse_references
from app.retrieval.service import Candidate
from app.retrieval.snapshot import Chunk, PublicationSnapshot
from app.retrieval.text import STOPWORDS, stem, words

Relation = Literal["requested", "retrieved", "definition", "reference"]
_OTHER_LAW = re.compile(r"\s*,?\s*(?:of|in|under)\s+(?:the\s+|that\s+|any\s+other\s+)?(?!this\b|said\b|same\b)\S", re.I)
_REFERENCE_SOURCES = 2          # follow references only from the best-ranked provisions


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    key: str                    # the id the model cites: "E1"
    chunk: Chunk
    relation: Relation

    @property
    def evidence_id(self) -> str:
        """Stable public identifier used in API responses."""
        return f"chunk:{self.chunk.id}"


def build_context(snapshot: PublicationSnapshot, candidates: Sequence[Candidate],
                  analysis: QueryAnalysis, settings) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    used: set[tuple[UUID, UUID]] = set()
    size = 0

    def add(key: tuple[UUID, UUID], relation: Relation, *, force: bool = False) -> bool:
        nonlocal size
        if key in used:
            return True
        parts = snapshot.chunks_by_root.get(key)
        if not parts:
            return False
        cost = sum(len(snapshot.display[part.index]) for part in parts)
        if not force and (len(units) + len(parts) > settings.qa_context_max_units
                          or size + cost > settings.qa_context_max_chars):
            return False
        used.add(key)
        size += cost
        units.extend(EvidenceUnit(f"E{len(units) + offset + 1}", part, relation)
                     for offset, part in enumerate(parts))
        return True

    exact = bool(candidates) and all(item.exact_rank is not None for item in candidates)
    roots = list(dict.fromkeys((item.chunk.instrument_id, item.chunk.root_provision_id)
                               for item in candidates))
    if not exact:
        roots = roots[:settings.qa_rerank_top_k]
    for position, key in enumerate(roots):
        # The best provision is always shown, even if it alone exceeds the budget.
        add(key, "requested" if exact else "retrieved", force=position == 0)

    # Definitions the question depends on.
    question_stems = {stem(word) for word in words(analysis.search_text)}
    added = 0
    for instrument_id in dict.fromkeys(key[0] for key in roots[:3]):
        if added >= settings.qa_context_max_definition_sections:
            break
        entry = snapshot.definitions.get(instrument_id)
        if entry is None or entry[0] in used:
            continue
        key, terms = entry
        matched = False
        for term in terms:
            content = [part for part in term if part not in STOPWORDS]
            if content and all(part in question_stems for part in content):
                matched = matched or len(content) >= 2 or analysis.wants_definition
        if matched and add(key, "definition"):
            added += 1

    # Provisions the best-ranked text points to within the same law.
    followed = 0
    for key in roots[:_REFERENCE_SOURCES]:
        if followed >= settings.qa_context_max_reference_sections:
            break
        for part in snapshot.chunks_by_root.get(key, ()):
            text = snapshot.display[part.index]
            for start, end in parse_references(text).spans:
                if _OTHER_LAW.match(text, end):
                    continue                    # "section 5 of the Code": another law
                for reference in parse_references(text[start:end]).references:
                    if followed >= settings.qa_context_max_reference_sections:
                        break
                    target = snapshot.find_root(key[0], reference.kind, reference.label)
                    if target is not None and target not in used and add(target, "reference"):
                        followed += 1
    return units
