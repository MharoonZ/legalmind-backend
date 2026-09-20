"""Build the grounded prompt sent to the LLM.

Two rules shape this file:

1. The model may only use the passages we hand it. It has no tools, no memory
   and no browsing, so anything it cannot find in the evidence block it must
   refuse.
2. Every claim must carry an `evidence_id` that we supplied. Any other ID is
   thrown away later in `app.services.citations`, so inventing one gains the
   model nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.fusion import FusedHit

MAX_QUOTE_WORDS = 45

SYSTEM_PROMPT = f"""\
You are LegalMind, a careful legal research assistant for the Contract Act, \
1872 of Pakistan.

You will receive a user question and a numbered EVIDENCE list. Each evidence \
item is verbatim statutory text with an evidence_id.

Rules you must follow:
1. Answer ONLY from the EVIDENCE. Never use outside legal knowledge, other \
statutes, case law, or assumptions.
2. If the EVIDENCE does not clearly answer the question, set "supported" to \
false, leave "citations" empty, and say plainly that the provided sections do \
not cover it.
3. Every citation's "quote" must be copied character for character from the \
evidence item with that evidence_id. Never paraphrase inside a quote, never \
join text from two items, never add ellipses that are not in the source.
4. Keep each quote to the shortest passage that proves the point \
(at most {MAX_QUOTE_WORDS} words).
5. Use only evidence_id values that appear in the EVIDENCE list.
6. Write the answer in plain English, 2 to 4 sentences, for someone who is not \
a lawyer. State what the law says; do not give personal legal advice or \
predict the outcome of a dispute.
7. Cite the most relevant evidence first. Use at most 3 citations.
8. Return JSON only, matching the required schema. No markdown, no commentary.\
"""

_EVIDENCE_TEMPLATE = """\
[{index}] evidence_id: {evidence_id}
law: {law_name} ({jurisdiction})
section: {section_number} - {section_title}
pdf_page: {page_number}
text: \"\"\"
{text}
\"\"\"
"""


def build_evidence_block(hits: Sequence[FusedHit]) -> str:
    """Render retrieved passages in a fixed, machine-readable layout."""
    return "\n".join(
        _EVIDENCE_TEMPLATE.format(
            index=index,
            evidence_id=hit.passage.evidence_id,
            law_name=hit.passage.law_name,
            jurisdiction=hit.passage.jurisdiction,
            section_number=hit.passage.section_number,
            section_title=hit.passage.section_title,
            page_number=hit.passage.page_number,
            text=hit.passage.text.strip(),
        )
        for index, hit in enumerate(hits, start=1)
    )


def build_user_prompt(question: str, hits: Sequence[FusedHit]) -> str:
    """The user turn: evidence first, then the question, then the reminder."""
    return (
        "EVIDENCE\n"
        "========\n"
        f"{build_evidence_block(hits)}\n"
        "QUESTION\n"
        "========\n"
        f"{question.strip()}\n\n"
        "Answer using only the EVIDENCE above. If it is not enough, set "
        '"supported" to false.'
    )
