"""Stage 12: assemble the evidence the answer model may use.

Each selected provision is included whole (every part of a split section), in
rank order, until the budget is reached. When there is room, three kinds of
related text from the same law are added: its definitions, the provisions the
selected text refers to, and the provisions that refer to it (the penalty or
exception clause that names it only by number). Assembly never fails a
question: whatever does not fit is simply left out.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np

from app.retrieval.query import QueryAnalysis, has_topic
from app.retrieval.service import Candidate
from app.retrieval.snapshot import Chunk, PublicationSnapshot, RootKey, readable
from app.retrieval.text import STOPWORDS, stem, words

Relation = Literal["requested", "retrieved", "definition", "reference", "referring"]
_REFERENCE_SOURCES = 2          # follow references only from the best-ranked provisions


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    key: str                    # the id the model cites: "E1"
    chunk: Chunk
    relation: Relation
    excerpt: str | None = None  # Only when one published chunk alone exceeds the prompt budget.

    @property
    def evidence_id(self) -> str:
        """Stable public identifier used in API responses."""
        return f"chunk:{self.chunk.id}"


def _by_relevance(snapshot: PublicationSnapshot, keys: Sequence[RootKey], text: str) -> list[RootKey]:
    """``keys`` ordered by keyword relevance to ``text``; document order breaks ties."""
    allowed = np.zeros(len(snapshot.chunks), dtype=bool)
    for key in keys:
        for part in snapshot.chunks_by_root[key]:
            allowed[part.index] = True
    terms, _ = snapshot.lexical.query_terms(text)
    best: dict[RootKey, float] = {}
    for position, score in snapshot.lexical.search(terms, mask=allowed, limit=int(allowed.sum())):
        chunk = snapshot.chunks[position]
        root = (chunk.instrument_id, chunk.root_provision_id)
        best[root] = max(best.get(root, 0.0), score)
    return sorted(keys, key=lambda key: -best.get(key, 0.0))


def build_context(snapshot: PublicationSnapshot, candidates: Sequence[Candidate],
                  analysis: QueryAnalysis, settings) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    used: set[RootKey] = set()
    size = 0

    def add(key: RootKey, relation: Relation, *, best: Chunk | None = None) -> bool:
        nonlocal size
        if key in used:
            return True
        parts = snapshot.chunks_by_root.get(key)
        if not parts:
            return False
        room = settings.qa_context_max_chars - size
        slots = settings.qa_context_max_units - len(units)
        cost = sum(len(snapshot.display[part.index]) for part in parts)
        if len(parts) > slots or cost > room:
            if best is None:
                return False
            center = next(i for i, part in enumerate(parts) if part.id == best.id)
            selected = []
            for part in sorted(range(len(parts)), key=lambda i: (abs(i - center), i)):
                length = len(snapshot.display[parts[part].index])
                if length > room or len(selected) == slots:
                    break
                selected.append(part)
                room -= length
            if not selected:
                # Restrict both the prompt and quote verification to the same raw prefix.
                body = best.text[best.body_start:best.body_start + room]
                if " " in body:
                    body = body.rsplit(" ", 1)[0]
                bounded = replace(best, text=best.text[:best.body_start] + body)
                excerpt = readable(body)
                units.append(EvidenceUnit(f"E{len(units) + 1}", bounded, relation, excerpt))
                size += len(excerpt)
                used.add(key)
                return True
            parts = [parts[i] for i in sorted(selected)]
            cost = sum(len(snapshot.display[part.index]) for part in parts)
        used.add(key)
        size += cost
        first = len(units) + 1          # fixed before extending: the list grows as it is filled
        units.extend([EvidenceUnit(f"E{first + offset}", part, relation)
                      for offset, part in enumerate(parts)])
        return True

    exact = bool(candidates) and all(item.exact_rank is not None for item in candidates)
    roots = list(dict.fromkeys((item.chunk.instrument_id, item.chunk.root_provision_id)
                               for item in candidates))
    if not exact:
        roots = roots[:settings.qa_rerank_top_k]
    for position, key in enumerate(roots):
        add(key, "requested" if exact else "retrieved", best=candidates[0].chunk if position == 0 else None)

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
        for target in snapshot.outgoing.get(key, ()):
            if followed >= settings.qa_context_max_reference_sections:
                break
            if target not in used and add(target, "reference"):
                followed += 1

    # Provisions that point at the best-ranked text: usually the penalty,
    # exception or procedure clause, which names it only by number. A request
    # for the bare text of a provision ("what is section 5?") does not need them.
    limit = settings.qa_context_max_referring_sections
    if limit and (not exact or has_topic(analysis, snapshot)):
        sources = list(dict.fromkeys(
            source for key in roots[:_REFERENCE_SOURCES] for source in snapshot.referrers.get(key, ())
            if source not in used))
        if len(sources) > limit:
            sources = _by_relevance(snapshot, sources, analysis.search_text)
        referring = 0
        for source in sources:
            if referring >= limit:
                break
            if add(source, "referring"):
                referring += 1
    return units
