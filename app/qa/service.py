"""POST /ask: retrieve, rerank, assemble evidence, generate and ground an answer.

The pipeline prefers a useful, cited answer over a refusal. Deterministic
replies (small talk, "which law?", "that section does not exist") skip the
model entirely; otherwise one model call writes the answer and the server
verifies its citations against the published text. If generation fails, the
service can return named provisions or a verified excerpt matching the topic.

One request works to one deadline: every model exchange is given no more than
the time that is left, so the reply (or the text-only fallback) is ready before
the route's own timeout would discard it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.qa.citations import GroundedAnswer, ground_answer, topic_excerpt, without_citations
from app.qa.context import EvidenceUnit, build_context
from app.qa.llm import AnswerGenerator, GenerationInvalid, GenerationUnavailable
from app.qa.prompt import (
    ANSWER_SCHEMA, REWRITE_PROMPT, REWRITE_SCHEMA, SYSTEM_PROMPT, build_answer_prompt,
    build_rewrite_prompt,
)
from app.qa.rerank import CrossEncoderReranker
from app.qa.smalltalk import small_talk
from app.retrieval.query import topic_words
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
_MODEL_DOWN_NOTE = ("The answer model is temporarily unavailable, so this reply quotes the requested "
                    "text without explaining it.")
_MAX_LISTED_LAWS = 12
# Time kept back from the request limit to ground, compose and send the reply.
_REPLY_MARGIN_SECONDS = 1.5
_MIN_REWRITE_SECONDS = 1.0      # below this a restatement is not worth starting
_MIN_GENERATION_SECONDS = 2.0   # below this an answer call could not finish in time
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
                 generator: AnswerGenerator, settings, *, recorder=None,
                 clock: Callable[[], float] = perf_counter):
        self.retrieval = retrieval
        self.reranker = reranker
        self.generator = generator
        self.settings = settings
        self.recorder = recorder
        self._clock = clock

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

    async def _retrieve(self, question: str, request: AskIn, history, deadline: float) -> RetrievalResult:
        context = request.context
        return await self.retrieval.retrieve(
            question, limit=max(10, self.settings.qa_rerank_candidates), law_hint=request.law,
            embedding_budget=max(0, deadline - self._clock()) / 2,
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

    async def _rewrite(self, question: str, history, timeout: float, generate) -> str | None:
        try:
            raw = await generate(
                REWRITE_PROMPT, build_rewrite_prompt(question, history), REWRITE_SCHEMA,
                max_output_tokens=256, purpose="rewrite", timeout=timeout)
        except (GenerationUnavailable, GenerationInvalid):
            return None
        rewritten = normalize(str(raw.get("question", "")))[:1000]
        return rewritten if rewritten and rewritten.casefold() != question.casefold() else None

    # ----------------------------------------------------------------- ask
    async def ask(self, request: AskIn) -> tuple[AskOut, dict[str, float]]:
        """Answer one question. Returns the response and per-stage milliseconds."""
        timings: dict[str, float] = {}
        started = self._clock()
        deadline = started + self.settings.qa_request_timeout_seconds - _REPLY_MARGIN_SECONDS
        question = normalize(request.question)
        retrieval: RetrievalResult | None = None
        units: list[EvidenceUnit] = []
        llm_calls = 0
        method = "none"

        async def generate(*args, **kwargs):
            nonlocal llm_calls
            llm_calls += 1
            return await self.generator.generate_json(*args, **kwargs)

        try:
            snapshot = await self.retrieval.index.ensure_loaded()
            reply = small_talk(question, snapshot)
            if reply is not None:
                result = AskOut(status="answered", question=request.question, answer=reply,
                                publication_id=snapshot.id if snapshot else None)
                return result, self._finish(result, timings, started, llm_calls, method)
            history = self._history(request)

            stage = self._clock()
            retrieval = await self._retrieve(question, request, history, deadline)
            timings["retrieval"] = (self._clock() - stage) * 1000
            rewritten = None
            # The restatement is optional: it gets a short limit of its own and is
            # skipped when too little of the request's time is left to finish one.
            if self._weak(retrieval) and deadline - self._clock() >= _MIN_REWRITE_SECONDS:
                stage = self._clock()
                rewritten = await self._rewrite(question, history, min(
                    self.settings.qa_rewrite_timeout_seconds, deadline - self._clock()), generate)
                if rewritten:
                    second = await self._retrieve(rewritten, request, history, deadline)
                    if self._strength(second) > self._strength(retrieval):
                        retrieval = second
                    else:
                        rewritten = None
                timings["rewrite"] = (self._clock() - stage) * 1000

            result = self._deterministic(request.question, retrieval)
            if result is None:
                snapshot = retrieval.snapshot
                stage = self._clock()
                # Reranking is optional. Leave time for generation or a cited extract
                # even if the inference worker is still loading or scoring a model.
                budget = max(0, deadline - self._clock()) / 2
                if self.settings.qa_rerank_mode == "auto":
                    budget = min(budget, self.settings.qa_rerank_budget_ms / 1000)
                try:
                    async with asyncio.timeout(budget):
                        ordered, method = await self.reranker.rerank(
                            retrieval.analysis.search_text, retrieval.candidates, snapshot)
                except TimeoutError:
                    ordered, method = retrieval.candidates, "fused"
                    logger.info("qa_rerank_timeout using_retrieval_order")
                timings["rerank"] = (self._clock() - stage) * 1000
                units = build_context(snapshot, ordered, retrieval.analysis, self.settings)
                stage = self._clock()
                try:
                    result = await self._generate(request.question, retrieval, units, history, rewritten,
                                                  deadline, generate)
                finally:
                    timings["generation"] = (self._clock() - stage) * 1000
        except (GenerationUnavailable, GenerationInvalid) as exc:
            logger.warning("qa_generation_unavailable error=%s", exc)
            result = self._text_only(request.question, retrieval, units) or self.unavailable(
                request.question, self._publication(retrieval), str(exc))
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

    def _finish(self, result: AskOut, timings, started, llm_calls, method) -> dict[str, float]:
        timings["total"] = (self._clock() - started) * 1000
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

    def _text_only(self, question: str, retrieval: RetrievalResult | None,
                   units: Sequence[EvidenceUnit]) -> AskOut | None:
        """Serve published wording when generation is unavailable."""
        if retrieval is None or retrieval.status != "ok" or retrieval.unknown_law:
            return None
        snapshot = retrieval.snapshot
        if not retrieval.analysis.has_reference:
            terms = topic_words(retrieval.analysis, snapshot)
            terms = [retrieval.spelling_repairs.get(word, word) for word in terms]
            scoped = [unit for unit in units if not retrieval.analysis.hard_scope
                      or unit.chunk.instrument_id in retrieval.analysis.scope]
            excerpt = topic_excerpt(terms, scoped, snapshot)
            if excerpt is None:
                return None
            return AskOut(status="partial", question=question, publication_id=snapshot.id,
                          answer=excerpt.answer, claims=excerpt.claims, citations=excerpt.citations,
                          missing_support="I found this passage, but the answer model is unavailable to explain how it applies to your situation.",
                          limitations=(_INFORMATION_NOTE, _EDITION_NOTE))
        requested = [unit for unit in units if unit.relation == "requested"]
        titles = []
        for unit in requested:
            chunk = unit.chunk
            title = f"{chunk.citation} of {snapshot.instruments[chunk.instrument_id].title}"
            if chunk.root_total_chunks > 1:
                title += f" (part {chunk.sequence_in_root} of {chunk.root_total_chunks})"
            titles.append(title)
        # Ground the headings only. Statutory punctuation, brackets and layout
        # in each published display passage are copied directly into the reply.
        headings = "\n".join(f"{title} [{unit.key}]" for title, unit in zip(titles, requested, strict=True))
        grounded = ground_answer(headings, [], requested, snapshot)
        if not grounded.citations:
            return None
        numbers = {citation.evidence_id: i for i, citation in enumerate(grounded.citations, 1)}
        parts = [f"{title} [{numbers[unit.evidence_id]}]\n{snapshot.display[unit.chunk.index]}"
                 for title, unit in zip(titles, requested, strict=True) if unit.evidence_id in numbers]
        missing = _MODEL_DOWN_NOTE
        if retrieval.missing_references:
            missing += " " + (retrieval.message or "Could not find " + ", ".join(retrieval.missing_references) + ".")
        return AskOut(
            status="partial", question=question, publication_id=snapshot.id,
            answer="\n\n".join([missing, *parts]), claims=grounded.claims,
            citations=grounded.citations, missing_support=missing,
            limitations=(_INFORMATION_NOTE, _EDITION_NOTE))

    # ------------------------------------------------------------ generation
    async def _generate(self, question: str, retrieval: RetrievalResult, units: Sequence[EvidenceUnit],
                        history, rewritten: str | None, deadline: float, generate) -> AskOut:
        snapshot = retrieval.snapshot
        if not self.generator.configured:
            raise GenerationUnavailable("The answer model is not configured on the server.")
        feedback = None
        draft: _AnswerDraft | None = None
        grounded: GroundedAnswer | None = None
        for _ in range(1 + self.settings.qa_repair_attempts):
            left = deadline - self._clock()
            if left < _MIN_GENERATION_SECONDS:
                if draft is None:
                    raise GenerationUnavailable("The answer request ran out of time.")
                break                           # no time to correct the draft: it is returned as it is
            prompt = build_answer_prompt(snapshot, retrieval, units, question, history,
                                         rewritten=rewritten, feedback=feedback)
            raw = await generate(SYSTEM_PROMPT, prompt, ANSWER_SCHEMA, timeout=left)
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
        return self._compose(question, retrieval, draft, grounded)

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
            answer = without_citations(draft.answer) or ("I couldn't find an answer to this in the laws available in this collection. "
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
