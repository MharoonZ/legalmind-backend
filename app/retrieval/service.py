"""First-stage retrieval over the active publication snapshot.

Exact provision lookup, BM25 keyword search and dense vector search run in
memory and are merged with Reciprocal Rank Fusion. Every path degrades
gracefully: an unreadable reference, an unnamed law or an unavailable encoder
narrows what can be found, but never turns a question into an error.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal
from uuid import UUID

import numpy as np

from app.core.inference import InferenceRuntime, runtime as default_runtime
from app.embeddings.profiles import EmbeddingProfile
from app.retrieval.index import PublicationIndex
from app.retrieval.query import QueryAnalysis, analyze, has_topic
from app.retrieval.references import Reference
from app.retrieval.snapshot import Chunk, PublicationSnapshot
from app.schemas.legal_search import CandidateOut, LegalSearchIn, LegalSearchOut

logger = logging.getLogger(__name__)

_MAX_EXACT_ROOTS = 12
_OVERVIEW_ROOTS = 5
_CONTEXT_BIAS = 2.0          # extra fusion weight for the law carried over from the conversation
_INFERENCE_MARGIN = 1.5      # how clearly one law must lead before it is assumed
Status = Literal["ok", "no_results", "needs_clarification", "no_active_publication", "service_unavailable"]


@dataclass(slots=True)
class Candidate:
    chunk: Chunk
    rank: int = 0
    exact_rank: int | None = None
    keyword_rank: int | None = None
    vector_rank: int | None = None
    rrf_rank: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    rrf_score: float = 0.0
    reranker_score: float | None = None
    matched_reference_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class RetrievalResult:
    status: Status
    query: str
    analysis: QueryAnalysis | None = None
    snapshot: PublicationSnapshot | None = None
    candidates: list[Candidate] = field(default_factory=list)
    message: str | None = None
    clarification_options: list[dict] = field(default_factory=list)
    missing_references: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    overview: bool = False                      # "tell me about <law>": no topic beyond the law itself
    inferred_law: UUID | None = None            # assumed from the topic, not named by the user
    unknown_law: str | None = None              # a named law that is not in the collection
    spelling_repairs: dict[str, str] = field(default_factory=dict)

    def to_response(self) -> LegalSearchOut:
        snapshot = self.snapshot
        results = []
        if snapshot is not None:
            for candidate in self.candidates:
                chunk = candidate.chunk
                law = snapshot.instruments[chunk.instrument_id]
                results.append(CandidateOut(
                    chunk_id=chunk.id, chunk_set_id=law.chunk_set_id, revision_id=law.revision_id,
                    root_provision_id=chunk.root_provision_id, instrument_id=law.id,
                    legal_version_id=law.legal_version_id, instrument_title=law.title,
                    citation_key=law.citation_key, edition_key=law.edition_key,
                    jurisdiction_code=law.jurisdiction_code,
                    applicability_status=law.applicability_status, kind=chunk.kind,
                    label=chunk.label, heading=chunk.heading, reference_path=chunk.reference_path,
                    matched_reference_paths=list(candidate.matched_reference_paths),
                    sequence_in_root=chunk.sequence_in_root, root_total_chunks=chunk.root_total_chunks,
                    chunk_text=chunk.text, source_refs=[s.as_source_ref() for s in chunk.segments],
                    rank=candidate.rank, exact_rank=candidate.exact_rank,
                    keyword_rank=candidate.keyword_rank, vector_rank=candidate.vector_rank,
                    rrf_rank=candidate.rrf_rank, keyword_score=candidate.keyword_score,
                    vector_score=candidate.vector_score, rrf_score=candidate.rrf_score))
        analysis = self.analysis
        return LegalSearchOut(
            status=self.status, query=self.query,
            retrieval_query=analysis.search_text if analysis else self.query,
            publication_id=snapshot.id if snapshot else None,
            publication_name=snapshot.name if snapshot else None,
            context_used=bool(analysis and analysis.context_used), message=self.message,
            clarification_options=self.clarification_options, warnings=self.warnings,
            missing_references=self.missing_references, results=results)


class QueryEncoder:
    """Encode questions with the publication's embedding profile; cache repeats."""

    def __init__(self, *, runtime: InferenceRuntime | None = None, cache_size: int = 2048, encode=None):
        self._runtime = runtime or default_runtime
        self._cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        self._cache_size = cache_size
        self._encode = encode

    def _run(self, text: str, profile: EmbeddingProfile) -> np.ndarray:
        if self._encode is not None:
            return np.asarray(self._encode(text, profile), dtype=np.float32)
        from app.embeddings.encoder import encode_texts
        return np.asarray(encode_texts([text], profile, batch_size=1)[0], dtype=np.float32)

    async def warm(self, profile: EmbeddingProfile) -> None:
        await self.encode("warm up", profile)

    async def encode(self, text: str, profile: EmbeddingProfile) -> np.ndarray | None:
        key = (profile.name, text)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        started = perf_counter()
        try:
            vector = await self._runtime.run(lambda stop: self._run(text, profile), stage="query_embedding")
        except Exception as exc:        # model missing, queue full: keyword search still works
            logger.warning("query_encoding_failed error=%s", type(exc).__name__)
            return None
        norm = float(np.linalg.norm(vector))
        if vector.shape != (profile.dimension,) or not np.isfinite(vector).all() or norm == 0:
            logger.warning("query_encoding_invalid")
            return None
        vector = vector / norm
        self._cache[key] = vector
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        logger.info("query_encoding elapsed_ms=%.0f", (perf_counter() - started) * 1000)
        return vector


class RetrievalService:
    def __init__(self, index: PublicationIndex, settings, *, encoder: QueryEncoder | None = None):
        self.index = index
        self.settings = settings
        self.encoder = encoder or QueryEncoder()

    # ---------------------------------------------------------------- API
    async def search(self, request: LegalSearchIn) -> LegalSearchOut:
        """``POST /search/legal``: ranked evidence candidates, no generated answer."""
        context = request.context
        reference = None
        if request.reference is not None:
            from app.retrieval.references import parse_references
            parsed = parse_references(f"{request.reference.kind} {request.reference.label}")
            reference = parsed.references[0] if parsed.references else None
        result = await self.retrieve(
            request.query, limit=request.limit, law_hint=request.law,
            instrument_id=request.instrument_id, jurisdiction=request.jurisdiction_code,
            reference=reference, previous_query=context.previous_query if context else None,
            context_law=context.law if context else None,
            context_instrument_id=context.instrument_id if context else None)
        return result.to_response()

    async def retrieve(self, question: str, *, limit: int = 10, law_hint: str | None = None,
                       instrument_id: UUID | None = None, jurisdiction: str | None = None,
                       reference: Reference | None = None, previous_query: str | None = None,
                       context_law: str | None = None, context_instrument_id: UUID | None = None,
                       history: Sequence[tuple[str, str]] = ()) -> RetrievalResult:
        started = perf_counter()
        try:
            snapshot = await self.index.ensure_loaded()
            if snapshot is None:
                return RetrievalResult("no_active_publication", question,
                    message="No complete active corpus publication is available.")
            analysis = analyze(
                snapshot, question, law_hint=law_hint, instrument_id=instrument_id,
                jurisdiction=jurisdiction, reference=reference, previous_query=previous_query,
                context_law=context_law, context_instrument_id=context_instrument_id, history=history)
            if analysis.has_reference:
                result = await self._exact(snapshot, analysis, question, limit)
            else:
                result = await self._hybrid(snapshot, analysis, question, limit)
        except Exception as exc:
            logger.exception("legal_retrieval_failed error=%s", type(exc).__name__)
            return RetrievalResult("service_unavailable", question,
                message="The published evidence search is temporarily unavailable.")
        logger.info(
            "legal_retrieval status=%s mode=%s law=%s source=%s candidates=%d repairs=%d elapsed_ms=%.0f",
            result.status, "exact" if analysis.has_reference else "hybrid", analysis.law.status,
            analysis.law_source, len(result.candidates), len(result.spelling_repairs),
            (perf_counter() - started) * 1000)
        return result

    # -------------------------------------------------------------- hybrid
    async def _ranked(self, snapshot: PublicationSnapshot, text: str, *, scope: Sequence[UUID] | None,
                      bias: Sequence[UUID] | None = None, limit: int, jurisdiction: str | None = None,
                      ) -> tuple[list[Candidate], dict[str, str], bool]:
        """Fused keyword + vector ranking. Returns candidates, repairs, semantic availability."""
        pool = max(limit, self.settings.legal_search_candidate_pool)
        allowed = None
        if scope:
            allowed = snapshot.mask(scope)
        elif jurisdiction:
            allowed = snapshot.mask(law.id for law in snapshot.instruments.values()
                                    if law.jurisdiction_code.casefold() == jurisdiction.casefold())
        terms, repairs = snapshot.lexical.query_terms(text)
        keyword = snapshot.lexical.search(terms, mask=allowed, limit=pool)
        vector = await self.encoder.encode(text, snapshot.profile)
        dense: list[tuple[int, float]] = []
        if vector is not None and len(snapshot.chunks):
            scores = snapshot.vectors @ vector
            if allowed is not None:
                scores = np.where(allowed, scores, -1.0)
            count = min(pool, scores.size)
            top = np.argpartition(-scores, count - 1)[:count]
            top = top[np.lexsort((top, -scores[top]))]
            floor = self.settings.legal_search_min_similarity
            dense = [(int(i), float(scores[i])) for i in top if scores[i] >= floor]
        merged: dict[int, Candidate] = {}
        k = self.settings.legal_search_rrf_k
        for rank, (doc, score) in enumerate(keyword, 1):
            item = merged.setdefault(doc, Candidate(snapshot.chunks[doc]))
            item.keyword_rank, item.keyword_score = rank, score
            item.rrf_score += 1.0 / (k + rank)
        for rank, (doc, score) in enumerate(dense, 1):
            item = merged.setdefault(doc, Candidate(snapshot.chunks[doc]))
            item.vector_rank, item.vector_score = rank, score
            item.rrf_score += 1.0 / (k + rank)
        if bias:
            preferred = set(bias)
            for item in merged.values():
                if item.chunk.instrument_id in preferred:
                    item.rrf_score *= _CONTEXT_BIAS
        ordered = sorted(merged.values(), key=lambda c: (
            -c.rrf_score, -(c.vector_score or -1.0), -(c.keyword_score or 0.0), c.chunk.index))
        for rank, item in enumerate(ordered, 1):
            item.rank = item.rrf_rank = rank
        return ordered[:limit], repairs, vector is not None

    async def _hybrid(self, snapshot, analysis: QueryAnalysis, question: str, limit: int) -> RetrievalResult:
        result = RetrievalResult("ok", question, analysis, snapshot)
        if analysis.law.status == "unresolved":
            result.unknown_law = analysis.law.mention
        if not has_topic(analysis, snapshot) and not analysis.context_used:
            if analysis.hard_scope and len(analysis.scope) == 1:
                # Only a law was named: show how it opens rather than guess a topic.
                law = analysis.scope[0]
                for rank, key in enumerate(snapshot.root_order.get(law, ())[:min(limit, _OVERVIEW_ROOTS)], 1):
                    result.candidates.append(Candidate(snapshot.chunks_by_root[key][0], rank=rank))
                result.overview = True
                if result.candidates:
                    return result
            elif not analysis.scope and analysis.law.status != "unresolved":
                result.status = "needs_clarification"
                result.message = "Which law or topic would you like to know about?"
                return result
        scope = analysis.scope if analysis.hard_scope else None
        bias = analysis.scope if analysis.scope and not analysis.hard_scope else None
        candidates, repairs, semantic = await self._ranked(
            snapshot, analysis.search_text, scope=scope, bias=bias, limit=limit,
            jurisdiction=analysis.jurisdiction)
        if not candidates and scope:
            # The named law has nothing on this topic: look across the collection.
            candidates, repairs, semantic = await self._ranked(
                snapshot, analysis.search_text, scope=None, limit=limit, jurisdiction=analysis.jurisdiction)
            if candidates:
                result.warnings.append("No matching passage in the named law; showing other laws.")
        result.candidates, result.spelling_repairs = candidates, repairs
        if not semantic:
            result.warnings.append("Semantic search unavailable; results use keyword search only.")
        if not candidates:
            result.status = "no_results"
            result.message = "No relevant passages were found in the published collection."
        return result

    # --------------------------------------------------------------- exact
    async def _infer_law(self, snapshot, analysis: QueryAnalysis, wanted: Sequence[tuple[str, str]],
                         ) -> tuple[UUID | None, list[UUID]]:
        """Pick the law a bare "section N" most plausibly belongs to, from the topic."""
        def has_all(instrument_id: UUID) -> bool:
            return all(snapshot.find_root(instrument_id, kind, label) for kind, label in wanted)

        holders = [law.id for law in snapshot.instruments.values() if has_all(law.id)
                   and (not analysis.jurisdiction or
                        law.jurisdiction_code.casefold() == analysis.jurisdiction.casefold())]
        if len(holders) == 1:
            return holders[0], holders
        if not holders or not has_topic(analysis):
            return None, holders
        ranked, _, _ = await self._ranked(snapshot, analysis.topical_text, scope=holders, limit=20)
        weight: dict[UUID, float] = {}
        for candidate in ranked:
            weight[candidate.chunk.instrument_id] = weight.get(candidate.chunk.instrument_id, 0.0) + candidate.rrf_score
        order = sorted(weight, key=lambda key: -weight[key])
        if order and (len(order) == 1 or weight[order[0]] >= _INFERENCE_MARGIN * weight[order[1]]):
            return order[0], order
        return None, order or holders

    def _options(self, snapshot, instrument_ids: Sequence[UUID]) -> list[dict]:
        return [{"instrument_id": str(key), "instrument_title": snapshot.instruments[key].title}
                for key in instrument_ids]

    async def _exact(self, snapshot, analysis: QueryAnalysis, question: str, limit: int) -> RetrievalResult:
        result = RetrievalResult("ok", question, analysis, snapshot)
        wanted = [(ref.kind, ref.label) for ref in analysis.references] + [
            (item.kind, label) for item in analysis.ranges for label in (item.start, item.end)]
        named = [ref.display for ref in analysis.references] + [item.display for item in analysis.ranges]

        if analysis.law.status == "unresolved":
            result.status, result.unknown_law = "no_results", analysis.law.mention
            result.missing_references = named
            result.message = (f"“{analysis.law.mention}” is not one of the laws in this collection, "
                              f"so {', '.join(named)} could not be looked up.")
            return result

        laws = list(analysis.scope)
        if len(laws) > 1:
            # Several laws fit the name: keep those that actually have the provision.
            holding = [key for key in laws if all(snapshot.find_root(key, kind, label) for kind, label in wanted)]
            if len(holding) == 1:
                laws = holding
            else:
                result.status = "needs_clarification"
                result.clarification_options = self._options(snapshot, holding or laws)
                result.message = "More than one law matches that name. Which one do you mean?"
                return result
        if not laws:
            inferred, ordered = await self._infer_law(snapshot, analysis, wanted)
            if inferred is None:
                result.status = "needs_clarification"
                result.clarification_options = self._options(snapshot, ordered[:6])
                result.message = f"Which law is {', '.join(named)} from?"
                return result
            laws, result.inferred_law = [inferred], inferred

        law = laws[0]
        roots: list[tuple[tuple[UUID, UUID], tuple[str, ...]]] = []
        for ref in analysis.references:
            key = snapshot.find_root(law, ref.kind, ref.label)
            if key is None:
                result.missing_references.append(ref.display)
                continue
            path = snapshot.child_path(key[1], ref.children) if ref.children else None
            root_path = snapshot.chunks_by_root[key][0].reference_path
            if ref.children and path is None:
                result.warnings.append(f"{ref.display}: that sub-provision was not found; showing the whole provision.")
            roots.append((key, (path or root_path,)))
        for item in analysis.ranges:
            keys = snapshot.roots_between(law, item.kind, item.start, item.end)
            if not keys:
                result.missing_references.append(item.display)
            else:
                absent = [label.upper() for label in (item.start, item.end)
                          if snapshot.find_root(law, item.kind, label) is None]
                if absent:
                    result.warnings.append(
                        f"{item.display}: this law has no {item.kind} {' or '.join(absent)}; "
                        "showing the provisions it does have in that range.")
            roots.extend((key, (snapshot.chunks_by_root[key][0].reference_path,)) for key in keys)

        seen: dict[tuple[UUID, UUID], tuple[str, ...]] = {}
        for key, paths in roots:
            seen[key] = tuple(dict.fromkeys(seen.get(key, ()) + paths))
        if len(seen) > _MAX_EXACT_ROOTS:
            result.warnings.append(f"Only the first {_MAX_EXACT_ROOTS} requested provisions are shown.")
        rank = 0
        for key, paths in list(seen.items())[:_MAX_EXACT_ROOTS]:
            for chunk in snapshot.chunks_by_root[key]:
                rank += 1
                result.candidates.append(Candidate(chunk, rank=rank, exact_rank=rank,
                                                   matched_reference_paths=paths))
        title = snapshot.instruments[law].title
        if not result.candidates:
            result.status = "no_results"
            result.message = f"{', '.join(result.missing_references)} could not be found in {title}."
        elif result.missing_references:
            result.message = f"{', '.join(result.missing_references)} could not be found in {title}."
        return result
