"""Deterministic legal-reference resolution and explicit follow-up context.

No LLM, inferred case facts, or invented Act aliases are involved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.legal_search import LegalReferenceIn, LegalSearchIn

_REF = re.compile(r"\b(?P<kind>sections?|sec\.?|s\.|rules?|articles?|art\.?)\s*"
                  r"(?P<label>\d+(?:-?[a-z]+)?(?:\s*\([a-z0-9]+\))*)", re.I)
_SCHEDULE = re.compile(r"\bschedule\s+([IVXLCDM]+|\d+[a-z]*)\b", re.I)
_FOLLOWUP = re.compile(r"^(?:and\b|also\b|what about\b|how about\b|then\b|does (?:it|that|this)\b|"
    r"can (?:it|that|this|they)\b|what if\b)|\b(?:(?:this|that|same)\s+"
    r"(?:act|law|ordinance|section|rule|article)|it|its)\b", re.I)
_GENERIC = {"the", "of", "and", "a", "an", "in", "under", "act", "ordinance", "rules", "law"}
_QUESTION_WORDS = {"what", "how", "can", "could", "would", "does", "do", "is", "are", "the",
    "a", "an", "i", "my", "me", "please", "tell", "explain", "about", "happen", "happens",
    "want", "know", "if", "it", "that", "this", "and", "also", "then"}


def normalized(value):
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def parse_references(query):
    refs = []
    for match in _REF.finditer(query):
        kind = match["kind"].lower()
        kind = "section" if kind.startswith("s") else "rule" if kind.startswith("r") else "article"
        refs.append(LegalReferenceIn(kind=kind, label=re.sub(r"\s+", "", match["label"])))
        tail = query[match.end():]
        continuation = re.match(r"\s*(?:,|and\b|or\b|to\b)\s*(\d+(?:-?[a-z]+)?(?:\([a-z0-9]+\))*)", tail, re.I)
        if continuation:
            refs.append(LegalReferenceIn(kind=kind, label=continuation[1]))
    refs.extend(LegalReferenceIn(kind="schedule", label=m[1]) for m in _SCHEDULE.finditer(query))
    unique = {(r.kind, r.label.casefold()): r for r in refs}
    return list(unique.values())


def reference_parts(reference):
    """Separate root identifier from child labels without prefix matching 19/19A."""
    match = re.fullmatch(r"([^()]+)((?:\([a-zA-Z0-9]+\))*)", reference.label)
    if match is None:
        raise ValueError("Invalid legal reference label")
    return match[1].strip(), re.findall(r"\(([^)]+)\)", match[2])


def resolve_laws(text, instruments, *, explicit=False):
    """Resolve only titles/aliases that actually occur in the active publication."""
    needle = normalized(text)
    if not needle:
        return []
    matches = []
    for instrument in instruments:
        names = [instrument["canonical_title"], instrument["citation_key"],
                 *[a for a in instrument["aliases"] if isinstance(a, str)]]
        for name in names:
            candidate = normalized(name)
            distinctive = set(candidate.split()) - _GENERIC
            # Allows omitted 'The', punctuation and reordered title words,
            # while preserving years and jurisdiction when explicitly supplied.
            present = set(needle.split())
            textual = f" {candidate} " in f" {needle} " or (explicit and needle in candidate)
            tokens = len(distinctive) >= 2 and distinctive <= present
            if candidate and (textual or tokens):
                matches.append(instrument)
                break
    return matches


def keyword_text(query):
    tokens = re.findall(r"\w+", query, re.UNICODE)
    return " ".join(t for t in tokens if t.casefold() not in _QUESTION_WORDS)


def explicit_law_from_reference(query):
    """Read a named law after 'of/under/from'; never invent a missing title."""
    parts = re.split(r"\b(?:of|under|from)\b", query, flags=re.I)
    for part in reversed(parts[1:]):
        match = re.match(r"\s*(?:the\s+)?(.+?\b(?:act|ordinance|rules|code|regulations?|constitution|order)\b(?:[,\s]+\d{4})?)", part, re.I)
        if match and not re.match(r"(?:section|article|rule)\b", match[1], re.I):
            if re.match(r"(?:this|that|same)\s", match[1], re.I):
                return None
            return match[1].strip()
    return None


@dataclass
class PreparedQuery:
    text: str
    context_used: bool
    followup: bool
    reference: LegalReferenceIn | None
    multiple_references: bool


def prepare_query(request: LegalSearchIn):
    followup = bool(_FOLLOWUP.search(request.query))
    previous = request.context.previous_query if request.context else None
    context_used = bool(followup and previous)
    text = f"{previous}\n{request.query}" if context_used else request.query
    refs = [request.reference] if request.reference else parse_references(request.query)
    # A previous reference is not silently imposed on a new 'what if' question;
    # its text can help semantic retrieval, while explicit new refs take priority.
    return PreparedQuery(text, context_used, followup, refs[0] if len(refs) == 1 else None, len(refs) > 1)
