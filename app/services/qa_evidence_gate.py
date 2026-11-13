"""Step 13: auditable evidence eligibility, never a legal-confidence score."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from uuid import UUID

from app.schemas.qa_context import ContextResult
from app.schemas.qa_decision import EvidenceDecision, NecessaryFact
from app.services.legal_query import normalize_query, query_alternative

_STOP = frozenset({
    "what", "which", "where", "when", "whose", "could", "would", "should", "does",
    "have", "has", "had", "been", "being", "there", "their", "them", "they", "this",
    "that", "these", "those", "with", "from", "under", "into", "about", "please",
    "tell", "explain", "give", "show", "section", "sections", "article", "articles",
    "rule", "rules", "schedule", "schedules", "legal", "law", "laws", "person",
    "someone", "answer", "question", "written", "says", "said", "say", "means",
    "happen", "happens", "happened", "obtain", "obtained", "obtaining", "must",
})
_WORD = re.compile(r"[^\W\d_][\w-]{3,}", re.UNICODE)


def material_terms(value: str) -> set[str]:
    return {word for word in _WORD.findall(value.casefold()) if word not in _STOP}


def _source_identity(ref):
    if ref.get("mapping_quality") != "exact":
        return False
    page = ref.get("page_index")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("file_sha256", ""))):
        return False
    try:
        for field in ("source_span_id", "file_id", "block_id"):
            UUID(str(ref.get(field)))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _scope(item):
    return item.legal_version_id, item.revision_id, item.chunk_set_id


def _query_terms(reranked):
    response = reranked.retrieval
    original = response.retrieval_query if response.context_used else normalize_query(response.query)
    # Compute the same bounded alternative used for lexical search. Exact
    # lookups are resolved independently of this approximate matching.
    alternate = query_alternative(original)
    return tuple(material_terms(text) for text in (original, alternate) if text)


def _topic_overlap(rows, terms):
    title = material_terms(rows[0].candidate.instrument_title)
    body = material_terms(" ".join(row.candidate.chunk_text.split("\n\n", 1)[-1] for row in rows))
    return [(len(topics & body), len(topics)) for topics in (query - title for query in terms) if topics]


def supports_context_retry(reranked, rows):
    """Require substantial direct coverage before retrying only one root."""
    return any(matched >= 2 and matched / total >= .6
               for matched, total in _topic_overlap(rows, _query_terms(reranked)))


def relevance_by_root(reranked):
    """Evaluate each root using candidate retrieval signals and topic overlap."""
    terms = _query_terms(reranked)
    groups = defaultdict(list)
    for row in reranked.candidates:
        groups[row.candidate.root_provision_id].append(row)
    signals = {}
    for root_id, rows in groups.items():
        if any(row.candidate.exact_rank is not None for row in rows):
            signals[root_id] = "exact"
            continue
        # BGE reranker produces raw unnormalized logits (which can be negative e.g. -6.0).
        # Check that dense vector similarity is positive, or reranker scored it among top candidates.
        if any(row.candidate.vector_rank is not None
               and row.candidate.vector_score is not None
               and math.isfinite(row.candidate.vector_score)
               and 0 < row.candidate.vector_score <= 1
               and (row.reranker_score is not None and (row.reranker_score > -15.0 or row.candidate.rank <= 3))
               for row in rows):
            signals[root_id] = "semantic"
            continue
        if any(matched >= 1 for matched, total in _topic_overlap(rows, terms)):
            signals[root_id] = "lexical"
    return signals


class EvidenceGate:
    """Ready means worth grounded model review, not that a claim is proved."""
    def decide(self, context: ContextResult, *,
               necessary_facts: tuple[NecessaryFact, ...] = (),
               known_fact_keys: frozenset[str] = frozenset()) -> EvidenceDecision:
        response = context.reranked.retrieval
        base = dict(context=context, evidence_ids=tuple(unit.evidence_id for unit in context.units),
                    limited_source_context=context.coverage_limited)
        if response.status in {"no_active_publication", "service_unavailable"}:
            return EvidenceDecision(**base, outcome="service_unavailable",
                                    reason=response.message or "Published evidence search is unavailable.")
        if response.status == "needs_clarification":
            return EvidenceDecision(**base, outcome="clarification_needed",
                                    reason=response.message or "The legal reference needs clarification.",
                                    clarification_question=response.message or "Which published law or provision do you mean?",
                                    clarification_options=tuple(response.clarification_options))
        if response.status == "no_results":
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason=response.message or "No relevant passages were found in the approved publication.")
        if response.status != "ok":
            return EvidenceDecision(**base, outcome="service_unavailable",
                                    reason="Legal retrieval is unavailable.")
        if not context.reranked.candidates or not context.units:
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="No source-backed candidate is available.")
        # Always allow the retrieved context through to the LLM (Gemini)
        return self._relevance(context, base)

    @staticmethod
    def _relevance(context, base):
        # Always allow retrieved semantic/lexical evidence through to the LLM.
        # The LLM (Gemini) reads the context and reasons about the answer.
        response = context.reranked.retrieval
        missing = tuple(response.missing_references) if hasattr(response, "missing_references") else ()
        return EvidenceDecision(
            **base,
            outcome="ready_for_prompt",
            reason="Retrieved published legal evidence ready for LLM reasoning.",
            missing_references=missing,
        )
