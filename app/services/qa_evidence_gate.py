"""Step 13: auditable evidence eligibility, never a legal-confidence score."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from uuid import UUID

from app.schemas.qa_context import ContextResult
from app.schemas.qa_decision import EvidenceDecision, NecessaryFact

_STOP = frozenset({
    "what", "which", "where", "when", "whose", "could", "would", "should", "does",
    "have", "has", "had", "been", "being", "there", "their", "them", "they", "this",
    "that", "these", "those", "with", "from", "under", "into", "about", "please",
    "tell", "explain", "give", "show", "section", "sections", "article", "articles",
    "rule", "rules", "schedule", "schedules", "legal", "law", "laws", "person",
    "someone", "answer", "question", "written", "says", "said", "say", "means",
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


class EvidenceGate:
    """Ready means worth grounded model review, not that a claim is proved."""
    def decide(self, context: ContextResult, *,
               necessary_facts: tuple[NecessaryFact, ...] = (),
               known_fact_keys: frozenset[str] = frozenset()) -> EvidenceDecision:
        response = context.reranked.retrieval
        base = dict(context=context, evidence_ids=tuple(unit.evidence_id for unit in context.units))
        if response.status == "no_active_publication":
            return EvidenceDecision(**base, outcome="service_unavailable",
                                    reason="No complete active legal publication is available.")
        if response.status == "needs_clarification":
            return EvidenceDecision(**base, outcome="clarification_needed",
                                    reason=response.message or "The legal reference needs clarification.",
                                    clarification_question=response.message or "Which published law or provision do you mean?",
                                    clarification_options=tuple(response.clarification_options))
        if response.status == "no_results":
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="No relevant passages were returned from the approved publication.")
        if response.status != "ok":
            return EvidenceDecision(**base, outcome="service_unavailable",
                                    reason="Legal retrieval is unavailable.")
        if not context.reranked.candidates or not context.units:
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="No complete source-backed candidate is available.")
        if any(issue.critical for issue in context.issues):
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="Required source context or publication evidence is missing.")
        if not any(unit.relation == "retrieved" for unit in context.units):
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="The retrieved passage was omitted from the context budget.")
        if any(unit.publication_id != response.publication_id or not unit.source_refs or
               not unit.text.strip() or not all(_source_identity(ref) for ref in unit.source_refs)
               for unit in context.units):
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="A context passage lacks exact source provenance.")

        if len(set(base["evidence_ids"])) != len(base["evidence_ids"]):
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="Context contains duplicate evidence identities.")
        scope_by_law, retained = {}, {}
        for unit in context.units:
            identity = unit.chunk_id if unit.source_kind == "chunk" else unit.provision_id
            if identity is None or unit.evidence_id != f"{unit.source_kind}:{identity}":
                return EvidenceDecision(**base, outcome="insufficient_evidence",
                                        reason="A context evidence identity is inconsistent.")
            scope = _scope(unit)
            if unit.instrument_id in scope_by_law and scope_by_law[unit.instrument_id] != scope:
                return EvidenceDecision(**base, outcome="insufficient_evidence",
                                        reason="Context mixes source versions for one law.")
            scope_by_law[unit.instrument_id] = scope
            if unit.relation == "retrieved":
                retained[unit.chunk_id] = unit
        selected = context.reranked.candidates
        if len({row.candidate.chunk_id for row in selected}) != len(selected) or set(retained) != {
                row.candidate.chunk_id for row in selected}:
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="Selected candidates and retained evidence do not agree.")
        for row in selected:
            candidate = row.candidate
            unit = retained[candidate.chunk_id]
            if (row.publication_id != response.publication_id or unit.source_kind != "chunk" or
                    unit.instrument_id != candidate.instrument_id or _scope(unit) != _scope(candidate) or
                    unit.provision_id != candidate.root_provision_id or unit.text != candidate.chunk_text or
                    candidate.exact_rank is not None and candidate.exact_rank < 1):
                return EvidenceDecision(**base, outcome="insufficient_evidence",
                                        reason="A retained passage differs from its selected source identity.")

        # Relevance must pass before a reviewed fact can trigger a personal
        # question. An arbitrary retained ID is not a relevance signal.
        ready = self._relevance(context, base)
        if ready.outcome != "ready_for_prompt":
            return ready
        available = {unit.evidence_id: unit for unit in context.units}
        if len({fact.key for fact in necessary_facts}) != len(necessary_facts):
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="Fact requirements contain conflicting identities.")
        for requirement in necessary_facts:
            if not set(requirement.evidence_ids) <= available.keys() or not any(
                    " ".join(requirement.quote.split()) in " ".join(available[key].text.split())
                    for key in requirement.evidence_ids):
                return EvidenceDecision(**base, outcome="insufficient_evidence",
                                        reason="A claimed necessary user fact has no source-backed rule in this context.")
        missing = [fact for fact in necessary_facts if fact.key not in known_fact_keys]
        if missing:
            fact = missing[0]
            evidence_id = next(key for key in fact.evidence_ids if
                               " ".join(fact.quote.split()) in " ".join(available[key].text.split()))
            return EvidenceDecision(**base, outcome="clarification_needed",
                                    reason="A published rule requires a missing user fact.",
                                    clarification_question=fact.question,
                                    clarification_fact=fact, clarification_evidence_id=evidence_id,
                                    heuristic_notes=ready.heuristic_notes + (
                                        "The fact requirement must come from a reviewed server-side rule; its necessity is not inferred by this gate.",))
        return ready

    @staticmethod
    def _relevance(context, base):
        response = context.reranked.retrieval
        if any(row.candidate.exact_rank is not None for row in context.reranked.candidates):
            return EvidenceDecision(**base, outcome="ready_for_prompt",
                                    reason="An exact published reference has complete source context for model review.")
        # Phase B has already applied its configured vector-similarity filter.
        # A matching dense candidate AND a positive raw cross-encoder score can
        # admit a paraphrase for grounded review, without requiring shared words.
        # Neither signal establishes legal support or a calibrated confidence.
        semantic_match = any(
            row.candidate.vector_rank is not None and
            row.candidate.vector_score is not None and
            math.isfinite(row.candidate.vector_score) and
            0 < row.candidate.vector_score <= 1 and row.reranker_score > 0
            for row in context.reranked.candidates
        )
        if semantic_match:
            return EvidenceDecision(**base, outcome="ready_for_prompt",
                                    reason="Published semantic evidence also passed cross-encoder relevance screening.",
                                    heuristic_notes=("This unvalidated vector/reranker screen allows model review; it does not prove legal support and needs corpus evaluation.",))
        # Keep a conservative lexical route when semantic search is unavailable.
        # Compare passage bodies, excluding title/display prefixes.
        title_words = set().union(*(material_terms(unit.instrument_title) for unit in context.units))
        query_words = material_terms(response.query) - title_words
        # Split fragments of the same section can contribute jointly. Unrelated
        # sections/laws must not manufacture relevance by pooling isolated words.
        bodies = defaultdict(list)
        for unit in context.units:
            if unit.relation == "retrieved":
                bodies[(unit.instrument_id, unit.revision_id, unit.provision_id)].append(
                    unit.text.split("\n\n", 1)[-1])
        required_overlap = min(2, len(query_words))
        if not required_overlap or not any(
                len(query_words & material_terms(" ".join(parts))) >= required_overlap
                for parts in bodies.values()):
            return EvidenceDecision(**base, outcome="insufficient_evidence",
                                    reason="The retrieved passages do not show enough direct topic overlap for model review.",
                                    heuristic_notes=("Lexical overlap is an unvalidated screening heuristic; it may miss synonyms.",))
        return EvidenceDecision(**base, outcome="ready_for_prompt",
                                reason="Source-backed passages show topic overlap for grounded model review.",
                                heuristic_notes=("Lexical overlap is an unvalidated screening heuristic, not legal support or a probability.",))
