"""Resolve queries, retrieve only published evidence, and fuse lexical/dense ranks."""
from __future__ import annotations

import logging

from app.embeddings.encoder import encode_texts_async
from app.embeddings.profiles import ACTIVE_PROFILE
from app.repositories.legal_search_repository import PublicationChanged
from app.schemas.legal_search import LegalSearchOut
from app.services.legal_query import explicit_law_from_reference, keyword_text, prepare_query, resolve_laws
from app.services.retrieval_fusion import reciprocal_rank_fusion

logger = logging.getLogger(__name__)


class LegalRetrievalService:
    def __init__(self, repository, settings, *, encode=encode_texts_async):
        self.repository, self.settings, self.encode = repository, settings, encode

    async def search(self, request):
        prepared = prepare_query(request)
        response = {"query": request.query, "retrieval_query": prepared.text,
                    "context_used": prepared.context_used}
        for attempt in range(2):
            publication, instruments = await self.repository.active_catalog()
            if publication is None:
                return LegalSearchOut(status="no_active_publication", **response,
                    message="No complete active corpus publication is available.")
            response.update(publication_id=publication["id"], publication_name=publication["name"])
            if request.jurisdiction_code:
                instruments = [i for i in instruments if i["jurisdiction_code"].casefold() == request.jurisdiction_code.casefold()]
            instrument_id = request.instrument_id
            context = request.context if prepared.followup else None
            explicit_law = request.law or explicit_law_from_reference(request.query)
            law_text = explicit_law or request.query
            matches = resolve_laws(law_text, instruments, explicit=bool(explicit_law))
            if not matches and not explicit_law and context and context.law:
                explicit_law = context.law
                matches = resolve_laws(explicit_law, instruments, explicit=True)
            if not matches and not explicit_law and prepared.context_used:
                matches = resolve_laws(request.context.previous_query, instruments)
            if explicit_law and not matches:
                return LegalSearchOut(status="no_results", **response,
                    message="The specified law was not resolved in this publication.")
            if len(matches) > 1:
                return LegalSearchOut(status="needs_clarification", **response,
                    message="More than one published law matches; specify an instrument ID.",
                    clarification_options=matches)
            if matches:
                if instrument_id is not None and instrument_id != matches[0]["instrument_id"]:
                    return LegalSearchOut(status="needs_clarification", **response,
                        message="The supplied law and instrument ID conflict.")
                instrument_id = matches[0]["instrument_id"]
            if instrument_id is None and context and context.instrument_id:
                instrument_id = context.instrument_id
                response["context_used"] = True
            if instrument_id is not None and not any(i["instrument_id"] == instrument_id for i in instruments):
                return LegalSearchOut(status="no_results", **response,
                    message="The selected instrument is not available in this publication and jurisdiction.")
            if prepared.multiple_references:
                return LegalSearchOut(status="needs_clarification", **response,
                    message="Specify one legal reference per lookup.")
            if prepared.reference is None and prepared.followup and not prepared.context_used and len(keyword_text(request.query).split()) < 2:
                return LegalSearchOut(status="needs_clarification", **response,
                    message="Provide the earlier question or identify the law and legal issue.")
            query_vector, warnings = None, []
            if prepared.reference is None:
                try:
                    query_vector = (await self.encode([prepared.text], ACTIVE_PROFILE, batch_size=1))[0]
                except Exception:
                    logger.exception("Semantic query encoding unavailable; returning lexical evidence only")
                    warnings.append("Semantic search unavailable; results use PostgreSQL full-text search only.")
            try:
                exact, keyword, vector = await self.repository.retrieve(publication["id"],
                    instrument_id=instrument_id, jurisdiction=request.jurisdiction_code,
                    query=keyword_text(prepared.text), reference=prepared.reference,
                    query_vector=query_vector, pool_size=self.settings.legal_search_candidate_pool,
                    min_similarity=self.settings.legal_search_min_similarity,
                    ef_search=self.settings.legal_search_hnsw_ef_search,
                    result_limit=request.limit, rrf_k=self.settings.legal_search_rrf_k)
            except PublicationChanged:
                if attempt == 0:
                    continue
                return LegalSearchOut(status="no_active_publication", **response,
                    message="Corpus availability changed; retry the query.")
            if prepared.reference is not None:
                paths = {(str(r["instrument_id"]), path) for r in exact
                         for path in r.get("matched_reference_paths", [r["reference_path"]])}
                if len(paths) > 1:
                    choices = {}
                    for row in exact:
                        for path in row["matched_reference_paths"]:
                            choices[(row["instrument_id"], path)] = {
                                "instrument_id": str(row["instrument_id"]),
                                "instrument_title": row["instrument_title"], "reference_path": path}
                    return LegalSearchOut(status="needs_clarification", **response,
                        message="This reference identifies multiple provisions; specify the law or source reference path.",
                        clarification_options=list(choices.values()))
                results = [{**r, "rank": n, "exact_rank": n} for n, r in enumerate(exact, 1)]
            else:
                results = reciprocal_rank_fusion(keyword, vector, k=self.settings.legal_search_rrf_k)
            return LegalSearchOut(status="ok" if results else "no_results", **response,
                warnings=warnings, results=results[:request.limit],
                message=None if results else "No relevant candidates were found in the selected published corpus.")
