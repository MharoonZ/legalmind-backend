"""POST /ask: retrieve, rerank, assemble evidence, generate and ground an answer.

The pipeline prefers a useful, cited answer over a refusal. Deterministic
replies (small talk, "which law?", "that section does not exist") skip the
model entirely; otherwise one model call writes the answer and the server
verifies its citations against the published text.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.qa.citations import GroundedAnswer, ground_answer
from app.qa.context import EvidenceUnit, build_context
from app.qa.llm import AnswerGenerator, GenerationInvalid, GenerationUnavailable
from app.qa.prompt import (
    ANSWER_SCHEMA, REWRITE_PROMPT, REWRITE_SCHEMA, SYSTEM_PROMPT, build_answer_prompt,
    build_rewrite_prompt,
)
from app.qa.rerank import CrossEncoderReranker
from app.qa.smalltalk import small_talk
from app.retrieval.service import RetrievalResult, RetrievalService
from app.retrieval.snapshot import PublicationSnapshot
from app.retrieval.text import normalize
from app.schemas.qa import AskIn, AskOut

logger = logging.getLogger(__name__)

_INFORMATION_NOTE = "This is general information from the cited legal text, not legal advice."
_EDITION_NOTE = "The collection does not record whether the cited edition is the version currently in force."
_COLLECTION_NOTE = "Answers are limited to the laws in this collection; other laws may also apply."
_UNLINKED_NOTE = "No source passage could be linked to this answer; check it against the law itself."
_NO_CITATION_FEEDBACK = (
    "Your previous answer did not cite any evidence passage that could be verified. Cite the passages "
    "you rely on as [E1], [E2] in the answer and copy a short exact quote from each into citations. "
    "If the evidence does not support an answer, return insufficient_evidence.")
_MAX_LISTED_LAWS = 12
_STATUSES = frozenset({"answered", "partial", "clarification_needed", "insufficient_evidence"})


class _DraftCitation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    evidence_id: str = ""
    quote: str = ""


class _AnswerDraft(BaseModel):
    """The model's untrusted output; everything is re-checked before it is returned."""
    model_config = ConfigDict(extra="ignore")
    status: Literal["answered", "partial", "clarification_needed", "insufficient_evidence"] = "answered"
    answer: str = ""
    citations: list[_DraftCitation] = Field(default_factory=list)
    clarification_question: str | None = None
    missing_support: str | None = None


class QuestionAnswerService:
    def __init__(self, retrieval: RetrievalService, reranker: CrossEncoderReranker,
                 generator: AnswerGenerator, settings, *, recorder=None):
        self.retrieval = retrieval
        self.reranker = reranker
        self.generator = generator
        self.settings = settings
        self.recorder = recorder

    # ------------------------------------------------------------- helpers
    @staticmethod
    def unavailable(question: str, publication_id=None, reason: str | None = None) -> AskOut:
        return AskOut(status="service_unavailable", question=question, publication_id=publication_id,
                      missing_support=reason,
                      answer="The answer service is temporarily unavailable. Please try again shortly.")

    @staticmethod
    def _laws_sentence(snapshot: PublicationSnapshot) -> str:
        titles = sorted(law.title for law in snapshot.instruments.values())
        shown = "; ".join(titles[:_MAX_LISTED_LAWS])
        more = f"; and {len(titles) - _MAX_LISTED_LAWS} more" if len(titles) > _MAX_LISTED_LAWS else ""
        return f"The collection currently covers: {shown}{more}."

    def _history(self, request: AskIn) -> list[tuple[str, str]]:
        turns = [(turn.role, turn.content) for turn in request.history]
        return turns[-self.settings.qa_history_max_turns:] if self.settings.qa_history_max_turns else []

    async def _retrieve(self, question: str, request: AskIn, history) -> RetrievalResult:
        context = request.context
        return await self.retrieval.retrieve(
            question, limit=max(10, self.settings.qa_rerank_candidates), law_hint=request.law,
            previous_query=context.previous_query if context else None,
            context_law=context.law if context else None,
            context_instrument_id=context.instrument_id if context else None, history=history)

    def _weak(self, retrieval: RetrievalResult) -> bool:
        """Retrieval that probably failed to understand the wording of the question."""
        analysis = retrieval.analysis
        if (not self.settings.qa_rewrite_enabled or not self.generator.configured or analysis is None
                or retrieval.status not in {"ok", "no_results"} or analysis.has_reference
                or retrieval.overview or analysis.law_source in {"question", "request"}):
            return False
        scores = [item.vector_score for item in retrieval.candidates if item.vector_score is not None]
        if not retrieval.candidates:
            return True
        return bool(scores) and max(scores) < self.settings.qa_rewrite_below_similarity

    @staticmethod
    def _strength(retrieval: RetrievalResult) -> float:
        if retrieval.status != "ok":
            return -1.0
        if retrieval.analysis is not None and retrieval.analysis.has_reference:
            return 2.0
        return max((item.vector_score or 0.0 for item in retrieval.candidates), default=0.0)

    async def _rewrite(self, question: str, history) -> str | None:
        try:
            raw = await self.generator.generate_json(
                REWRITE_PROMPT, build_rewrite_prompt(question, history), REWRITE_SCHEMA,
                max_output_tokens=256, purpose="rewrite")
        except (GenerationUnavailable, GenerationInvalid):
            return None
        rewritten = normalize(str(raw.get("question", "")))[:1000]
        return rewritten if rewritten and rewritten.casefold() != question.casefold() else None

    # ----------------------------------------------------------------- ask
    async def ask(self, request: AskIn) -> tuple[AskOut, dict[str, float]]:
        """Answer one question. Returns the response and per-stage milliseconds."""
        timings: dict[str, float] = {}
        started = perf_counter()
        question = normalize(request.question)
        retrieval: RetrievalResult | None = None
        llm_calls = 0
        method = "none"
        try:
            snapshot = await self.retrieval.index.ensure_loaded()
            reply = small_talk(question, snapshot)
            if reply is not None:
                result = AskOut(status="answered", question=request.question, answer=reply,
                                publication_id=snapshot.id if snapshot else None)
                return result, self._finish(result, timings, started, llm_calls, method)
            history = self._history(request)

            stage = perf_counter()
            retrieval = await self._retrieve(question, request, history)
            timings["retrieval"] = (perf_counter() - stage) * 1000
            rewritten = None
            if self._weak(retrieval):
                stage = perf_counter()
                rewritten = await self._rewrite(question, history)
                llm_calls += 1
                if rewritten:
                    second = await self._retrieve(rewritten, request, history)
                    if self._strength(second) > self._strength(retrieval):
                        retrieval = second
                    else:
                        rewritten = None
                timings["rewrite"] = (perf_counter() - stage) * 1000

            result = self._deterministic(request.question, retrieval)
            if result is None:
                snapshot = retrieval.snapshot
                stage = perf_counter()
                ordered, method = await self.reranker.rerank(
                    retrieval.analysis.search_text, retrieval.candidates, snapshot)
                timings["rerank"] = (perf_counter() - stage) * 1000
                units = build_context(snapshot, ordered, retrieval.analysis, self.settings)
                stage = perf_counter()
                result, calls = await self._generate(request.question, retrieval, units, history, rewritten)
                llm_calls += calls
                timings["generation"] = (perf_counter() - stage) * 1000
        except GenerationUnavailable as exc:
            logger.warning("qa_generation_unavailable error=%s", exc)
            result = self.unavailable(request.question, self._publication(retrieval), str(exc))
        except Exception as exc:
            logger.exception("qa_pipeline_failed error=%s", type(exc).__name__)
            result = self.unavailable(request.question, self._publication(retrieval))
        if self.recorder is not None and retrieval is not None:
            try:
                await self.recorder.record(request, result, retrieval, self.generator.model,
                                           self.settings.qa_rerank_model_name if method == "cross_encoder" else None)
            except Exception as exc:
                logger.warning("qa_recording_failed error=%s", type(exc).__name__)
        return result, self._finish(result, timings, started, llm_calls, method)

    @staticmethod
    def _publication(retrieval: RetrievalResult | None):
        return retrieval.snapshot.id if retrieval is not None and retrieval.snapshot is not None else None

    @staticmethod
    def _finish(result: AskOut, timings, started, llm_calls, method) -> dict[str, float]:
        timings["total"] = (perf_counter() - started) * 1000
        logger.info(
            "qa_request status=%s citations=%d llm_calls=%d ranking=%s %s", result.status,
            len(result.citations), llm_calls, method,
            " ".join(f"{name}_ms={value:.0f}" for name, value in timings.items()))
        return timings

    # --------------------------------------------------- deterministic paths
    def _deterministic(self, question: str, retrieval: RetrievalResult) -> AskOut | None:
        """Outcomes that are certain without asking the model."""
        snapshot = retrieval.snapshot
        publication_id = snapshot.id if snapshot else None
        if retrieval.status in {"no_active_publication", "service_unavailable"}:
            return self.unavailable(question, publication_id, retrieval.message)
        if retrieval.status == "needs_clarification":
            options = retrieval.clarification_options
            text = retrieval.message or "Which law do you mean?"
            if options:
                listed = "\n".join(f"{number}. {option['instrument_title']}"
                                   for number, option in enumerate(options, 1))
                text = f"{text}\n{listed}"
            return AskOut(status="clarification_needed", question=question, publication_id=publication_id,
                          answer=text, clarification_question=retrieval.message,
                          clarification_options=tuple(options), missing_support=retrieval.message)
        if retrieval.status == "no_results":
            analysis = retrieval.analysis
            parts = [retrieval.message or "I couldn't find this in the laws available in this collection."]
            if analysis is not None and analysis.has_reference and not retrieval.unknown_law and analysis.scope:
                labels = [label for label in snapshot.labels_of(analysis.scope[0]) if label]
                if labels:
                    parts.append(f"Its sections run from {labels[0]} to {labels[-1]}.")
            else:
                parts.append(self._laws_sentence(snapshot))
            return AskOut(status="insufficient_evidence", question=question, publication_id=publication_id,
                          answer=" ".join(parts), missing_support=retrieval.message,
                          limitations=(_COLLECTION_NOTE,))
        return None

    # ------------------------------------------------------------ generation
    async def _generate(self, question: str, retrieval: RetrievalResult, units: Sequence[EvidenceUnit],
                        history, rewritten: str | None) -> tuple[AskOut, int]:
        snapshot = retrieval.snapshot
        if not self.generator.configured:
            raise GenerationUnavailable("The answer model is not configured on the server.")
        feedback = None
        draft: _AnswerDraft | None = None
        grounded: GroundedAnswer | None = None
        calls = 0
        for _ in range(1 + self.settings.qa_repair_attempts):
            prompt = build_answer_prompt(snapshot, retrieval, units, question, history,
                                         rewritten=rewritten, feedback=feedback)
            raw = await self.generator.generate_json(SYSTEM_PROMPT, prompt, ANSWER_SCHEMA)
            calls += 1
            if raw.get("status") not in _STATUSES:
                raw = {**raw, "status": "answered" if raw.get("citations") else "insufficient_evidence"}
            try:
                draft = _AnswerDraft.model_validate(raw)
            except ValidationError:
                raise GenerationInvalid("The answer model returned an unusable draft.") from None
            grounded = ground_answer(draft.answer, [item.model_dump() for item in draft.citations],
                                     units, snapshot)
            needs_citation = draft.status in {"answered", "partial"}
            has_text = bool(draft.answer.strip()) or (
                draft.status == "clarification_needed" and bool((draft.clarification_question or "").strip()))
            if has_text and (grounded.citations or not needs_citation):
                break
            feedback = _NO_CITATION_FEEDBACK if has_text else "Your previous reply had no answer text."
        return self._compose(question, retrieval, draft, grounded), calls

    def _compose(self, question: str, retrieval: RetrievalResult, draft: _AnswerDraft,
                 grounded: GroundedAnswer) -> AskOut:
        snapshot = retrieval.snapshot
        status = draft.status
        missing = (draft.missing_support or "").strip() or None
        clarification = (draft.clarification_question or "").strip() or None
        answer = grounded.answer.strip()
        limitations: tuple[str, ...] = ()
        claims, citations = grounded.claims, grounded.citations
        if retrieval.missing_references and status == "answered":
            status = "partial"
        if retrieval.missing_references:
            missing = missing or (retrieval.message or "Not found: " + ", ".join(retrieval.missing_references))
        if status == "clarification_needed":
            clarification = clarification or answer or "Could you give a little more detail?"
            answer = answer or clarification
        elif status == "insufficient_evidence":
            claims, citations = (), ()
            answer = answer or ("I couldn't find an answer to this in the laws available in this collection. "
                                + self._laws_sentence(snapshot))
            limitations = (_COLLECTION_NOTE,)
        else:
            if not answer:
                status, answer = "insufficient_evidence", (
                    "I couldn't produce an answer from the available legal text. Please try rephrasing.")
            notes = [_INFORMATION_NOTE]
            if citations:
                cited = {citation.law for citation in citations}
                if any(law.title in cited and law.applicability_status != "verified"
                       for law in snapshot.instruments.values()):
                    notes.append(_EDITION_NOTE)
            else:
                notes.append(_UNLINKED_NOTE)
            limitations = tuple(notes)
        if status != "clarification_needed":
            clarification = None
        return AskOut(status=status, question=question, publication_id=snapshot.id, answer=answer,
                      claims=claims, citations=citations, clarification_question=clarification,
                      missing_support=missing, limitations=limitations)
