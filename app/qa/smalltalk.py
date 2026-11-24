"""Replies that need neither retrieval nor the answer model.

Only whole-message matches are handled here: a greeting followed by a legal
question goes through the normal pipeline.
"""
from __future__ import annotations

import re

from app.retrieval.snapshot import PublicationSnapshot

_GREETING = re.compile(
    r"^(?:hi+|hello+|hey+|hiya|salam|salaam|aoa|a\.?o\.?a\.?|as+alam[\s-]*[ou]?[\s-]*alaikum|"
    r"good\s+(?:morning|afternoon|evening|day)|greetings)(?:\s+(?:there|legalmind|everyone|sir|madam|bot))?"
    r"[\s!.,]*(?:how\s+are\s+you(?:\s+doing)?)?[\s!?.,]*$", re.I)
_THANKS = re.compile(
    r"^(?:ok(?:ay)?[\s,!.]*)?(?:thanks?(?:\s+you)?(?:\s+(?:so|very)\s+much|\s+a\s+lot)?|thx|ty|shukriya|"
    r"jazak\s?allah|great|perfect|nice|got\s+it|understood|ok(?:ay)?)[\s!.,]*$", re.I)
_FAREWELL = re.compile(r"^(?:bye+|good\s?bye|see\s+you(?:\s+later)?|(?:khuda|allah)\s+hafiz)[\s!.,]*$", re.I)
_ABOUT = re.compile(
    r"^(?:who|what)\s+are\s+you\??$|^what(?:'s|\s+is)\s+your\s+name\??$|^what\s+(?:can|do)\s+you\s+do\??$"
    r"|^how\s+(?:can|do)\s+you\s+help(?:\s+me)?\??$|^how\s+does\s+this\s+work\??$|^help[\s!?.]*$", re.I)
_CATALOG = re.compile(
    r"^(?:(?:can|could)\s+you\s+)?(?:please\s+)?(?:which|what|list|show|tell\s+me|give\s+me)\b.{0,40}\b"
    r"(?:laws?|acts?|ordinances?|documents?|statutes?|pdfs?)\b.{0,30}\b(?:available|have|has|cover(?:ed|s)?|"
    r"know|included?|contains?|support(?:ed|s)?|loaded|uploaded|(?:in|of)\s+(?:the|this|your)\s+"
    r"(?:collection|database|system|corpus))\b[\s?.!]*$"
    r"|^(?:list|show)(?:\s+me)?(?:\s+all)?(?:\s+the)?(?:\s+available)?\s+(?:laws|acts|documents)[\s?.!]*$", re.I)
_MAX_LISTED = 60


def _example(snapshot: PublicationSnapshot | None) -> str:
    if snapshot is None or not snapshot.instruments:
        return ""
    title = min(law.title for law in snapshot.instruments.values())
    return f" For example, ask about a topic, or about a provision such as “Section 3 of {title}”."


def small_talk(question: str, snapshot: PublicationSnapshot | None) -> str | None:
    text = " ".join(question.split())
    if _GREETING.fullmatch(text):
        return ("Hello! I can explain what the laws in this collection say. Tell me your question "
                "or describe what happened." + _example(snapshot))
    if _THANKS.fullmatch(text):
        return "You're welcome. Ask another question whenever you like."
    if _FAREWELL.fullmatch(text):
        return "Goodbye! Come back whenever you have another question."
    if _ABOUT.fullmatch(text):
        count = len(snapshot.instruments) if snapshot else 0
        scope = f" It currently holds {count} laws." if count else ""
        return ("I'm LegalMind. I answer questions from the official text of the laws in this "
                "collection and show the exact passages and PDF pages I relied on." + scope
                + _example(snapshot) + " I give legal information, not legal advice.")
    if _CATALOG.fullmatch(text) and snapshot is not None:
        titles = sorted(law.title for law in snapshot.instruments.values())
        shown = "\n".join(f"- {title}" for title in titles[:_MAX_LISTED])
        more = f"\n…and {len(titles) - _MAX_LISTED} more." if len(titles) > _MAX_LISTED else ""
        return f"This collection currently contains {len(titles)} laws:\n{shown}{more}"
    return None
