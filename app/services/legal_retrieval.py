"""Resolve queries, retrieve only published evidence, and fuse lexical/dense ranks."""
from __future__ import annotations

import logging
from itertools import zip_longest
from time import perf_counter

from sqlalchemy.exc import SQLAlchemyError

from app.embeddings.encoder import encode_texts_async
from app.embeddings.profiles import ACTIVE_PROFILE
from app.repositories.legal_search_repository import PublicationChanged
from app.schemas.legal_search import LegalSearchOut
from app.services.legal_query import explicit_law_mentions, keyword_text, normalize_query, prepare_query, resolve_laws
from app.services.retrieval_fusion import reciprocal_rank_fusion

logger = logging.getLogger(__name__)


class LegalRetrievalService:
    def __init__(self, repository, settings, *, encode=encode_texts_async):
        self.repository, self.settings, self.encode = repository, settings, encode

    async def search(self, request):
        started = perf_counter()
        try:
            result = await self._search(request)
        except (SQLAlchemyError, OSError, TimeoutError, ValueError) as exc:
            logger.warning("legal_retrieval_failed error=%s sqlstate=%s", type(exc).__name__,
                           getattr(getattr(exc, "orig", None), "sqlstate", None))
            result = LegalSearchOut(status="service_unavailable", query=request.query, retrieval_query=request.query,
                                    message="The published evidence search is temporarily unavailable.")
        logger.info("legal_retrieval status=%s candidates=%d publication=%s warnings=%d elapsed_ms=%.0f",
                    result.status, len(result.results), result.publication_id, len(result.warnings),
                    (perf_counter() - started) * 1000)
        return result

    async def _search(self, request):
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
            question = normalize_query(request.query)
            mentions = explicit_law_mentions(question)
            explicit_law = request.law or (mentions[-1] if mentions else None)
            law_text = explicit_law or question
            matches = resolve_laws(law_text, instruments, explicit=bool(explicit_law))
            selectors = set(mentions) | ({request.law} if request.law else set())
            if len(selectors) > 1:
                resolved = [resolve_laws(name, instruments, explicit=True) for name in selectors]
                if any(len(rows) != 1 for rows in resolved) or len({
                        rows[0]["instrument_id"] for rows in resolved}) != 1:
                    return LegalSearchOut(status="needs_clarification", **response,
                        message="The question names different laws. Specify the law for this lookup.")
            if explicit_law and prepared.references:
                # A trailing law name cannot be applied to references to
                # two different named laws in the same question.
                named = resolve_laws(question, instruments)
                if len({row["instrument_id"] for row in (*matches, *named)}) > 1:
                    matches = list({row["instrument_id"]: row for row in (*matches, *named)}.values())
            if not matches and not explicit_law and context and context.law:
                explicit_law = context.law
                matches = resolve_laws(explicit_law, instruments, explicit=True)
            if not matches and not explicit_law and context and context.previous_query:
                matches = resolve_laws(request.context.previous_query, instruments)
                response["context_used"] = bool(matches) or response["context_used"]
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
            if context and context.law and explicit_law == context.law:
                response["context_used"] = True
            if instrument_id is not None and not any(i["instrument_id"] == instrument_id for i in instruments):
                return LegalSearchOut(status="no_results", **response,
                    message="The selected instrument is not available in this publication and jurisdiction.")
            if (prepared.unsupported_reference or len(prepared.references) > min(4, request.limit)
                    or (len(prepared.references) > 1 and instrument_id is None)):
                return LegalSearchOut(status="needs_clarification", **response,
                    message="Specify the law and up to four individual legal references per lookup.")
            if (not prepared.references and prepared.followup and not prepared.context_used
                    and not (context and (context.law or context.instrument_id))
                    and len(keyword_text(question).split()) < 2):
                return LegalSearchOut(status="needs_clarification", **response,
                    message="Provide the earlier question or identify the law and legal issue.")
            common = dict(instrument_id=instrument_id, jurisdiction=request.jurisdiction_code,
                          pool_size=max(request.limit, self.settings.legal_search_candidate_pool),
                          min_similarity=self.settings.legal_search_min_similarity,
                          ef_search=self.settings.legal_search_hnsw_ef_search,
                          result_limit=request.limit, rrf_k=self.settings.legal_search_rrf_k)
            try:
                if prepared.references:
                    return await self._exact_lookup(prepared.references, response, common)
                return await self._hybrid_lookup(prepared, response, common,
                                                 prefer_alternative=request._prefer_semantic_alternative)
            except PublicationChanged:
                if attempt == 0:
                    continue
                return LegalSearchOut(status="no_active_publication", **response,
                    message="Corpus availability changed; retry the query.")

    async def _exact_lookup(self, references, response, common):
        lists, missing = [], []
        for reference in references:
            rows, _, _ = await self.repository.retrieve(response["publication_id"], **common,
                query="", reference=reference, query_vector=None)
            choices = {
                (row["instrument_id"], path): {
                    "instrument_id": str(row["instrument_id"]),
                    "instrument_title": row["instrument_title"], "reference_path": path,
                }
                for row in rows
                for path in row.get("matched_reference_paths", [row["reference_path"]])
            }
            if len(choices) > 1:
                return LegalSearchOut(status="needs_clarification", **response,
                    message="This reference identifies multiple provisions; specify the law or source reference path.",
                    clarification_options=list(choices.values()))
            if rows:
                lists.append(rows[:common["result_limit"]])
            else:
                missing.append(f"{reference.kind} {reference.label}")

        unique = {}
        for group in zip_longest(*lists):
            for row in group:
                if row is not None:
                    item = unique.setdefault(row["chunk_id"], {**row, "matched_reference_paths": []})
                    item["matched_reference_paths"] = list(dict.fromkeys(
                        item["matched_reference_paths"] + row.get("matched_reference_paths", [row["reference_path"]])))
        results = [{**row, "rank": rank, "exact_rank": rank}
                   for rank, row in enumerate(list(unique.values())[:common["result_limit"]], 1)]
        return LegalSearchOut(status="ok" if results else "no_results", **response, results=results,
            missing_references=missing,
            message=("Some requested references are unavailable in the selected publication." if results else
                     "The requested reference is unavailable in the selected publication.") if missing else None)

    async def _query_vector(self, query):
        started = perf_counter()
        try:
            vector, = await self.encode([query], ACTIVE_PROFILE, batch_size=1)
        except Exception as exc:
            logger.warning("legal_query_encoding_failed error=%s", type(exc).__name__)
            return None
        logger.info("legal_query_encoding elapsed_ms=%.0f", (perf_counter() - started) * 1000)
        return vector

    async def _hybrid_lookup(self, prepared, response, common, *, prefer_alternative=False):
        primary_keyword = keyword_text(prepared.text)
        alternative = keyword_text(prepared.alternative) if prepared.alternative else None
        semantic_query = prepared.alternative if prefer_alternative and prepared.alternative else prepared.text
        query_vector = await self._query_vector(semantic_query)
        started = perf_counter()
        _, keyword, vector = await self.repository.retrieve(response["publication_id"], **common,
            query=primary_keyword, alternative_keyword=alternative, reference=None, query_vector=query_vector)
        fallback = False
        if (not (keyword or vector) and query_vector is not None and not prefer_alternative
                and (prepared.alternative or common["instrument_id"] or common["jurisdiction"])):
            # Keep the original query independent. Re-encode only a bounded,
            # materially different alternative when the first pass found nothing.
            alternate_vector = await self._query_vector(prepared.alternative) if prepared.alternative else query_vector
            wider = {**common, "pool_size": min(100, 2 * common["pool_size"]),
                     "ef_search": min(1000, 2 * common["ef_search"])}
            if alternate_vector is not None and (prepared.alternative or wider != common):
                fallback = True
                _, keyword, vector = await self.repository.retrieve(response["publication_id"], **wider,
                    query=primary_keyword, alternative_keyword=alternative, reference=None, query_vector=alternate_vector)
        logger.info("legal_hybrid keyword=%d vector=%d alternative=%s fallback=%s elapsed_ms=%.0f",
                    len(keyword), len(vector), bool(alternative), fallback, (perf_counter() - started) * 1000)
        warnings = [] if query_vector is not None else [
            "Semantic search unavailable; results use PostgreSQL full-text search only."]
        results = reciprocal_rank_fusion(keyword, vector, k=common["rrf_k"])[:common["result_limit"]]
        if not results and warnings:
            return LegalSearchOut(status="service_unavailable", **response, warnings=warnings,
                                  message="Semantic search was unavailable and keyword search found no candidates.")
        if results and fallback:
            warnings.append("Evidence was found by the wider retrieval pass.")
        result = LegalSearchOut(status="ok" if results else "no_results", **response,
            warnings=warnings, results=results,
            message=None if results else "No relevant candidates were found in the selected published corpus.")
        result._has_semantic_alternative = bool(results and prepared.alternative and not fallback and not prefer_alternative)
        return result
