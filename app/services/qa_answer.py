"""Step 17: compose Phase C and return only verified, source-backed quotations."""
from __future__ import annotations

import logging
from time import perf_counter

from app.schemas.legal_search import LegalSearchIn
from app.schemas.qa import AskIn, AskOut
from app.schemas.qa_generation import AnswerDraft, DraftClaim
from app.services.qa_citations import CitationVerificationError
from app.services.qa_generation import GenerationInvalid, GenerationUnavailable
from app.services.qa_prompt import build_prompt

logger = logging.getLogger(__name__)


class QuestionAnswerService:
    def __init__(self, preparation, generator, verifier, *, repair_attempts: int = 1,
                 recorder=None):
        self.preparation = preparation
        self.generator = generator
        self.verifier = verifier
        self.repair_attempts = repair_attempts
        self.recorder = recorder

    async def ask(self, request: AskIn) -> AskOut:
        try:
            search = LegalSearchIn(query=request.question, limit=10, law=request.law, context=request.context)
            search._explicit_followup = request.context is not None
            decision = await self.preparation.prepare(search)
        except Exception as exc:
            logger.warning("qa_preparation_failed error=%s", type(exc).__name__)
            return self.unavailable(request.question)
        publication_id = (decision.context.reranked.retrieval.publication_id
                          if decision.context else None)
        if decision.outcome == "clarification_needed":
            if decision.clarification_fact is not None:
                fact = decision.clarification_fact
                try:
                    draft = AnswerDraft(outcome="clarification_needed", answer="",
                        clarification_question=fact.question, missing_support=decision.reason,
                        claims=[DraftClaim(evidence_id=decision.clarification_evidence_id,
                                           quote=fact.quote, claim_text=fact.quote)])
                    verified = await self.verifier.verify(draft, decision)
                except CitationVerificationError as exc:
                    if exc.code == "source_verification_unavailable":
                        return self.unavailable(request.question, publication_id)
                    return AskOut(status="insufficient_evidence", question=request.question,
                        publication_id=publication_id,
                        answer="I can't verify the source condition needed to ask a follow-up question.")
                except ValueError:
                    return self.unavailable(request.question, publication_id)
                result = AskOut(status="clarification_needed", question=request.question,
                    publication_id=publication_id,
                    answer=f'I need one more detail about your situation. The cited source says:\n“{fact.quote}”',
                    clarification_question=fact.question, missing_support=decision.reason,
                    claims=verified.claims, citations=verified.citations,
                    limitations=("The quotation shows the source condition; applicability still needs to be assessed.",))
            else:
                result = AskOut(status="clarification_needed", question=request.question,
                            answer="I need one more detail before checking the available law.",
                            publication_id=publication_id,
                            clarification_question=decision.clarification_question,
                            clarification_options=decision.clarification_options,
                            missing_support=decision.reason)
        elif decision.outcome == "insufficient_evidence":
            result = AskOut(status="insufficient_evidence", question=request.question,
                            publication_id=publication_id,
                            answer="I can't determine the answer from the approved PDFs currently in this collection. Try naming the Act or section, or check another applicable source.",
                            limitations=("The available corpus does not establish an answer; this does not mean the law is silent.",),
                            missing_support=decision.reason)
        elif decision.outcome == "service_unavailable":
            result = self.unavailable(request.question, publication_id, decision.reason)
        else:
            try:
                packet = build_prompt(decision)
                verified = None
                feedback = None
                for attempt in range(self.repair_attempts + 1):
                    started = perf_counter()
                    draft = await self.generator.generate(packet, repair_feedback=feedback)
                    logger.info("qa_generation outcome=%s attempt=%d elapsed_ms=%.0f", draft.outcome,
                                attempt + 1, (perf_counter() - started) * 1000)
                    started = perf_counter()
                    try:
                        verified = await self.verifier.verify(draft, decision)
                        logger.info("qa_citation_verification citations=%d elapsed_ms=%.0f",
                                    len(verified.citations), (perf_counter() - started) * 1000)
                        break
                    except CitationVerificationError as exc:
                        logger.info("qa_citation_rejected code=%s elapsed_ms=%.0f", exc.code,
                                    (perf_counter() - started) * 1000)
                        if exc.code == "source_verification_unavailable":
                            return self.unavailable(request.question, publication_id)
                        feedback = exc.code
                if verified is None:
                    result = AskOut(status="insufficient_evidence", question=request.question,
                                    publication_id=publication_id,
                                    answer="I can't verify a source-backed answer from the available PDFs.",
                                    limitations=("The generated draft failed quotation or citation checks.",))
                elif verified.outcome == "insufficient_evidence":
                    result = AskOut(status="insufficient_evidence", question=request.question,
                                    publication_id=publication_id,
                                    answer="I can't determine the answer from the available PDFs. The evidence supplied to the answer step did not support a reliable response.",
                                    missing_support=verified.missing_support or None,
                                    limitations=self._limitations(verified.missing_support, quotations=False))
                else:
                    leads = {
                        "answered": "The cited PDF text says:",
                        "partial": "I can't confirm a complete answer. A possibly relevant passage says:",
                        "clarification_needed": "I need one more detail about your situation. The cited PDF text says:",
                    }
                    lead = leads[verified.outcome]
                    quoted = "\n".join(f'“{claim.text}”' for claim in verified.claims)
                    result = AskOut(status=verified.outcome, question=request.question,
                                    publication_id=publication_id, answer=f"{lead}\n{quoted}",
                                    claims=verified.claims, citations=verified.citations,
                                    clarification_question=verified.clarification_question,
                                    missing_support=verified.missing_support or None,
                                    limitations=self._limitations(verified.missing_support))
            except (GenerationUnavailable, GenerationInvalid, ValueError) as exc:
                logger.warning("qa_generation_failed error=%s", type(exc).__name__)
                result = self.unavailable(request.question, publication_id)
        if self.recorder is not None:
            try:
                await self.recorder.record(request, result, decision)
            except Exception as exc:
                logger.warning("qa_recording_failed error=%s", type(exc).__name__)
                return self.unavailable(request.question, publication_id)
        return result

    @staticmethod
    def _limitations(missing_support, *, quotations=True):
        notes = (("These quotations show the available source text; applicability and other law are not established here."
                  if quotations else "Other law or missing facts may change the answer."),)
        if missing_support:
            # This is a model-identified information gap, not a verified legal
            # conclusion. Legal assertions remain exact, source-checked quotes.
            notes += (f"Information gap identified by the model: {missing_support}",)
        return notes

    @staticmethod
    def unavailable(question: str, publication_id=None, reason=None) -> AskOut:
        return AskOut(status="service_unavailable", question=question,
                      publication_id=publication_id, missing_support=reason,
                      answer="The answer service is temporarily unavailable. Please try again later.")
