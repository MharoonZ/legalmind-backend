"""Step 16: verify exact quotations and build citations from stored source spans."""
from __future__ import annotations

import re

from app.schemas.qa_decision import PreparedQuestion
from app.schemas.qa_generation import AnswerDraft
from app.schemas.qa_verified import VerifiedCitation, VerifiedClaim, VerifiedDraft
from app.services.qa.source_proofs import CitationVerificationError, covered_proofs


def _quote_span(text: str, quote: str) -> tuple[int, int] | None:
    # Only whitespace variation is allowed. Case, words, punctuation and
    # numbers must be identical to the pinned source text.
    parts = quote.strip().split()
    if not parts:
        return None
    # A literal substring of a word can reverse the quoted meaning (for
    # example, 'lawful' within 'unlawful') or change a numbered reference.
    left = r"(?<!\w)" if re.match(r"\w", parts[0][0]) else ""
    right = r"(?!\w)" if re.match(r"\w", parts[-1][-1]) else ""
    match = re.search(left + r"\s+".join(re.escape(part) for part in parts) + right, text)
    return match.span() if match else None


class CitationVerifier:
    def __init__(self, source_verifier):
        self.source_verifier = source_verifier

    async def verify(self, draft: AnswerDraft, decision: PreparedQuestion) -> VerifiedDraft:
        if decision.outcome != "ready_for_prompt" or decision.context is None:
            raise CitationVerificationError("context_not_ready")
        context = decision.context
        publication_id = context.reranked.retrieval.publication_id
        units = {unit.evidence_id: unit for unit in context.units}
        question = (draft.clarification_question or "").strip()
        missing_support = draft.missing_support.strip()
        if decision.missing_references:
            if draft.outcome not in {"partial", "insufficient_evidence"}:
                raise CitationVerificationError("missing_requested_reference")
            missing_support = "Unavailable in this publication: " + ", ".join(decision.missing_references) + "."
        elif decision.limited_source_context:
            if draft.outcome not in {"partial", "insufficient_evidence"}:
                raise CitationVerificationError("limited_source_context")
            missing_support = "Only some of the relevant material could be verified with its required source context."
        if draft.outcome == "clarification_needed":
            if len(question) < 3 or not missing_support:
                raise CitationVerificationError("missing_clarification")
        elif draft.clarification_question is not None:
            raise CitationVerificationError("unexpected_clarification")
        if draft.outcome == "partial" and not missing_support:
            raise CitationVerificationError("missing_limitation")
        if draft.outcome == "answered" and missing_support:
            raise CitationVerificationError("inconsistent_outcome")
        if draft.outcome == "insufficient_evidence":
            if draft.claims:
                raise CitationVerificationError("unsupported_claim")
            return VerifiedDraft(outcome=draft.outcome, claims=(), citations=(),
                                 missing_support=missing_support)
        if not draft.claims:
            raise CitationVerificationError("missing_claim")
        validated = []
        for claim in draft.claims:
            if claim.evidence_id not in decision.evidence_ids or claim.evidence_id not in units:
                raise CitationVerificationError("invalid_evidence_id")
            unit = units[claim.evidence_id]
            if unit.publication_id != publication_id:
                raise CitationVerificationError("wrong_publication")
            selected_item = next((item for item in context.reranked.candidates
                                  if item.candidate.instrument_id == unit.instrument_id), None)
            if selected_item and (selected_item.candidate.legal_version_id != unit.legal_version_id or
                                  selected_item.candidate.revision_id != unit.revision_id or
                                  selected_item.candidate.chunk_set_id != unit.chunk_set_id):
                raise CitationVerificationError("wrong_source_version")
            quote = claim.quote.strip()
            if len(quote) < 20 or " ".join(quote.split()) != " ".join(claim.claim_text.strip().split()):
                raise CitationVerificationError("unsupported_claim")
            span = _quote_span(unit.text, quote)
            if span is None:
                raise CitationVerificationError("quotation_mismatch")
            validated.append((unit, quote, span))
        try:
            proof_groups = await self.source_verifier.verify_many(
                [(unit, *span) for unit, _, span in validated])
        except CitationVerificationError:
            raise
        except Exception as exc:
            raise CitationVerificationError("source_verification_unavailable") from exc
        if len(proof_groups) != len(validated):
            raise CitationVerificationError("source_span_missing")
        citations, claims = [], []
        for (unit, quote, span), proofs in zip(validated, proof_groups):
            if not proofs:
                raise CitationVerificationError("source_span_missing")
            # Keep the final citation boundary independent of the source
            # adapter's implementation, including on the no-AI fact path.
            proofs = covered_proofs(unit.text, *span, proofs)
            indexes = []
            for proof in proofs:
                # A quotation can cross a PDF page or source block. Attribute
                # only the characters actually present in this source span.
                source_quote = unit.text[max(span[0], proof.start):min(span[1], proof.end)].strip()
                if not source_quote:
                    continue
                citation = VerifiedCitation(
                    evidence_id=unit.evidence_id, publication_id=unit.publication_id,
                    legal_version_id=unit.legal_version_id, revision_id=unit.revision_id,
                    provision_id=proof.provision_id, chunk_id=unit.chunk_id,
                    law=unit.instrument_title, edition=unit.edition_key,
                    jurisdiction=unit.jurisdiction_code, kind=proof.kind,
                    label=proof.label, reference_path=proof.reference_path,
                    quote=source_quote, source_span_id=proof.source_span_id,
                    file_id=proof.file_id, file_sha256=proof.file_sha256,
                    filename=proof.filename, page_index=proof.page_index,
                    source_url=proof.source_url,
                )
                if citation not in citations:
                    citations.append(citation)
                indexes.append(citations.index(citation))
            claims.append(VerifiedClaim(text=quote, evidence_id=unit.evidence_id,
                                        citation_indexes=tuple(dict.fromkeys(indexes))))
        requested = {path for row in context.reranked.candidates if row.candidate.exact_rank is not None
                     for path in row.candidate.matched_reference_paths}
        if requested and draft.outcome in {"answered", "partial"}:
            matched = {path for path in requested for citation in citations if citation.reference_path and
                       (citation.reference_path == path or citation.reference_path.startswith(path + "/"))}
            if not matched or (draft.outcome == "answered" and matched != requested):
                raise CitationVerificationError("requested_reference_not_cited")
        return VerifiedDraft(outcome=draft.outcome, claims=tuple(claims),
                             citations=tuple(citations), missing_support=missing_support,
                             clarification_question=question or None)
