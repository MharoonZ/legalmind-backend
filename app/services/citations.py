"""Verify the LLM's citations against the passages we actually retrieved.

Nothing the model returns is trusted:

* an `evidence_id` we did not send is dropped;
* a quote that is not in that passage is replaced with real text from it.

If no citation survives, the caller downgrades the answer to "unsupported".
This is the last gate before a legal claim reaches a user.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.repositories.law_repository import PassageHit

logger = logging.getLogger(__name__)

MIN_MATCH_CHARS = 30
MIN_FALLBACK_QUOTE_CHARS = 60
MAX_FALLBACK_QUOTE_CHARS = 320
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_END_RE = re.compile(r"(?<=[.;:])\s")


@dataclass(frozen=True, slots=True)
class ValidatedCitation:
    passage: PassageHit
    quote: str
    verbatim: bool  # False when we had to repair the model's quote


def normalise(value: str) -> str:
    """Collapse whitespace so line breaks in the PDF do not break matching."""
    return _WHITESPACE_RE.sub(" ", value).strip()


def _leading_sentence(text: str) -> str:
    """A deterministic, safe fallback quote: the passage's opening sentences.

    Statute text starts with its own number and marginal title ("29. Agreements
    void for uncertainty."), so stopping at the first full stop would quote
    "29." and nothing else. Keep adding sentences until the quote actually says
    something.
    """
    flat = normalise(text)
    if not flat:
        return ""

    quote = ""
    for piece in _SENTENCE_END_RE.split(flat):
        candidate = f"{quote} {piece}".strip() if quote else piece
        if len(candidate) > MAX_FALLBACK_QUOTE_CHARS:
            break
        quote = candidate
        if len(quote) >= MIN_FALLBACK_QUOTE_CHARS:
            break

    return quote or flat[:MAX_FALLBACK_QUOTE_CHARS].rstrip()


def repair_quote(claimed_quote: str, passage_text: str) -> tuple[str, bool]:
    """Return a quote that provably exists in the passage.

    1. Exact (whitespace-insensitive) match -> keep the model's quote.
    2. Long common run of text -> keep that run.
    3. Otherwise -> the passage's opening sentence.
    """
    flat_passage = normalise(passage_text)
    flat_quote = normalise(claimed_quote)

    if flat_quote and flat_quote.lower() in flat_passage.lower():
        start = flat_passage.lower().index(flat_quote.lower())
        return flat_passage[start : start + len(flat_quote)], True

    if flat_quote:
        matcher = SequenceMatcher(None, flat_passage.lower(), flat_quote.lower())
        block = matcher.find_longest_match(0, len(flat_passage), 0, len(flat_quote))
        if block.size >= MIN_MATCH_CHARS:
            return flat_passage[block.a : block.a + block.size].strip(), False

    return _leading_sentence(passage_text), False


def validate_citations(
    raw_citations: list[dict],
    retrieved: dict[str, PassageHit],
    *,
    max_citations: int = 3,
) -> list[ValidatedCitation]:
    """Keep only citations that point at retrieved passages, with real quotes."""
    validated: list[ValidatedCitation] = []
    seen: set[str] = set()

    for item in raw_citations:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id", "")).strip()
        passage = retrieved.get(evidence_id)
        if passage is None:
            logger.warning("Dropped citation with unknown evidence_id: %r", evidence_id)
            continue
        if evidence_id in seen:
            continue
        seen.add(evidence_id)

        quote, verbatim = repair_quote(str(item.get("quote", "")), passage.text)
        if not verbatim:
            logger.warning(
                "Repaired a non-verbatim quote for evidence_id=%s", evidence_id
            )
        validated.append(
            ValidatedCitation(passage=passage, quote=quote, verbatim=verbatim)
        )
        if len(validated) >= max_citations:
            break

    return validated
