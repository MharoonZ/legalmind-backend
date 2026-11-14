"""Deterministic legal-reference resolution and explicit follow-up context.

No LLM, inferred case facts, or invented Act aliases are involved.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.schemas.legal_search import LegalReferenceIn, LegalSearchIn

_NUMBERED_LABEL = r"\d{1,12}(?:-?[a-z]{1,6})?(?:\s*\(\s*[a-z0-9]{1,8}\s*\)){0,6}"
_REF_PREFIX = r"\b(?P<kind>(?:sub-?)?sections?|sec\.?|s\.|rules?|articles?|art\.?)\s*"
_REF = re.compile(_REF_PREFIX +
                  rf"(?P<label>{_NUMBERED_LABEL})(?![\w(])", re.I)
_REF_START = re.compile(_REF_PREFIX + r"(?=\d)", re.I)
_NEXT_REF = re.compile(rf"\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+|&\s*)"
                       rf"({_NUMBERED_LABEL})(?![\w(])", re.I)
_UNSUPPORTED_CONTINUATION = re.compile(
    r"\s*(?:(?:to\b|through\b|[-–—])\s*\d|(?:and\b|or\b|,|&)\s*\(|\s+\(|\[)", re.I)
_CHILD_PREFIX = re.compile(r"\bsub-?section\s*((?:\(\s*[a-z0-9]{1,8}\s*\)\s*){1,6})\s+of\s+(?:the\s+)?$", re.I)
_REF_TYPO = re.compile(r"\b(?:sectoin|seciton|secton|setcion)(?=\s*\d)", re.I)
_SCHEDULE = re.compile(r"\bschedule\s+([IVXLCDM]{1,20}|\d{1,12}[a-z]{0,6})\b", re.I)
_FOLLOWUP = re.compile(r"^(?:and\b|also\b|what about\b|how about\b|then\b|does (?:it|that|this)\b|"
    r"can (?:it|that|this|they)\b|what if\b)|\b(?:(?:this|that|same)\s+"
    r"(?:act|law|ordinance|section|rule|article)|it|its)\b", re.I)
_REFERENCE_FOLLOWUP = re.compile(r"\b(?:this|that|same)\s+(?:section|rule|article|schedule)\b|"
                                 r"^(?:yes|no)\b", re.I)
_GENERIC = {"the", "of", "and", "a", "an", "in", "under", "act", "ordinance", "rules", "law"}
_QUESTION_WORDS = {"what", "how", "can", "could", "would", "does", "do", "is", "are", "the",
    "a", "an", "i", "my", "me", "please", "tell", "explain", "about", "happen", "happens",
    "want", "know", "if", "it", "that", "this", "and", "also", "then", "follow", "information",
    "obtain", "obtained", "obtaining"}

_LEGAL_TERMS = frozenset("""
    agreement agreements appealed appeals application arbitration charged claimant clause clauses
    coercion consent consumer consumers contract contracts court creditor damaged damages deceived
    defective defendant disclosure dismissed document duties employee enforceable enforceability
    exception exceptions fraudulent fraud gambling goods heading hearing imprisonment influence inmate
    jurisdiction landlord lawful liability liable manufacturer misleading misrepresentation notice
    ordinance penalties penalty petition plaintiff pressure prison prisoner proceedings protection
    provision provisions purchase purchases purchaser purchasers receipt receipts regulation remedy remedies replacement
    respondent review shopkeeper statute subsection tenant termination unlawful valid voidable
    witness wrongful
""".split())
_TERM_ALIASES = {
    "proof of purchase": "receipt",
    "money back": "refund",
    "faulty product": "defective product",
    "jail officer": "prison officer",
    "inmate": "prisoner",
    "inmates": "prisoners",
    "forced to sign": "coerced into signing",
    "tricked into signing": "deceived into signing",
}
_KNOWN_TYPOS = {
    "agrement": "agreement", "challanged": "challenged", "coersion": "coercion",
    "conseqence": "consequence", "consequnces": "consequences", "fruad": "fraud",
    "happns": "happens", "obtaind": "obtained", "shopkepper": "shopkeeper",
}
_ASCII_WORD = re.compile(r"\b[a-z]{5,24}\b", re.I | re.ASCII)


def normalize_query(value: str) -> str:
    normalized_text = " ".join(unicodedata.normalize("NFKC", value).split())
    return normalized_text if len(normalized_text) <= 4000 else " ".join(value.split())


def _one_edit(left: str, right: str) -> bool:
    if len(left) == len(right):
        pairs = [(i, a, b) for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return (
            len(pairs) == 2 and pairs[1][0] == pairs[0][0] + 1
            and pairs[0][1] == pairs[1][2] and pairs[1][1] == pairs[0][2])
    short, long = sorted((left, right), key=len)
    if len(long) != len(short) + 1:
        return False
    first = next((i for i, (a, b) in enumerate(zip(short, long)) if a != b), len(short))
    return short[first:] == long[first + 1:]


def correct_legal_spelling(value: str) -> str:
    """Repair a bounded set of legal words without changing identifiers or years."""
    words = {word.lower() for word in _ASCII_WORD.findall(value)}
    if len(words) > 128:
        return value
    corrections = dict(_KNOWN_TYPOS)
    for word in words - _LEGAL_TERMS - _KNOWN_TYPOS.keys():
        if len(word) < 6:
            continue
        matches = [term for term in _LEGAL_TERMS if _one_edit(word, term)]
        if len(matches) == 1:
            corrections[word] = matches[0]
    return _ASCII_WORD.sub(lambda m: corrections.get(m[0].lower(), m[0]), value)


def query_alternative(value: str) -> str | None:
    """Suggest one retrieval variant. The original wording is searched independently."""
    repaired = correct_legal_spelling(value)
    for phrase, synonym in _TERM_ALIASES.items():
        repaired = re.sub(rf"\b{re.escape(phrase)}\b", synonym, repaired, flags=re.I)
    return repaired if repaired != value else None


def normalized(value):
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def _parse_references(query):
    query = _REF_TYPO.sub("section", normalize_query(query))
    refs, unsupported = [], False
    recognized = set()
    for match in _REF.finditer(query):
        recognized.add(match.start())
        kind = match["kind"].lower()
        kind = "section" if kind.startswith("s") else "rule" if kind.startswith("r") else "article"
        label = re.sub(r"\s+", "", match["label"])
        prefix = _CHILD_PREFIX.search(query[max(0, match.start() - 100):match.start()])
        if prefix and kind == "section":
            if "(" in label:
                unsupported = True
            else:
                label += re.sub(r"\s+", "", prefix[1])
        refs.append(LegalReferenceIn(kind=kind, label=label))
        end = match.end()
        while continuation := _NEXT_REF.match(query, end):
            refs.append(LegalReferenceIn(kind=kind, label=re.sub(r"\s+", "", continuation[1])))
            end = continuation.end()
        unsupported |= bool(_UNSUPPORTED_CONTINUATION.match(query, end))
    refs.extend(LegalReferenceIn(kind="schedule", label=m[1]) for m in _SCHEDULE.finditer(query))
    unique = {(r.kind, r.label.casefold()): r for r in refs}
    unsupported |= any(start.start() not in recognized for start in _REF_START.finditer(query))
    return list(unique.values()), unsupported


def parse_references(query):
    return _parse_references(query)[0]


def reference_parts(reference):
    """Separate root identifier from child labels without prefix matching 19/19A."""
    match = re.fullmatch(r"([^()]+)((?:\([a-zA-Z0-9]+\))*)", reference.label)
    if match is None:
        raise ValueError("Invalid legal reference label")
    return match[1].strip(), re.findall(r"\(([^)]+)\)", match[2])


def resolve_laws(text, instruments, *, explicit=False):
    """Resolve only titles/aliases that actually occur in the active publication."""
    matches = _match_laws(text, instruments, explicit=explicit)
    if explicit and not matches:
        corrected = correct_legal_spelling(text)
        if corrected != text:
            return _match_laws(corrected, instruments, explicit=True)
    return matches


def _match_laws(text, instruments, *, explicit):
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
            if explicit:
                textual = bool(present - _GENERIC) and f" {needle} " in f" {candidate} "
                tokens = bool(present - _GENERIC) and present - _GENERIC <= set(candidate.split())
                # A supplied Act/Ordinance/etc. type is part of the identity.
                types = {"act", "ordinance", "rules", "code", "constitution", "regulations", "order"}
                tokens = tokens and present & types <= set(candidate.split())
            else:
                textual = f" {candidate} " in f" {needle} "
                tokens = len(distinctive) >= 2 and distinctive <= present
            if candidate and (textual or tokens):
                matches.append(instrument)
                break
    return matches


def keyword_text(query):
    tokens = re.findall(r"\w+", query, re.UNICODE)
    return " ".join(t for t in tokens if t.casefold() not in _QUESTION_WORDS)


def explicit_law_mentions(query):
    """Read named laws from query (with prepositions of/under/from/in, or standalone title references)."""
    names = []
    consumed = 0
    # Preposition-anchored mentions: "Section 19 of Consumer Protection Act"
    for part in re.finditer(r"\b(?:of|under|from|in)\b", query, re.I):
        if part.start() < consumed:
            continue
        match = re.match(
            r"\s*(?:the\s+)?((?:(?!\b(?:under|from)\b)[^?;.\n]){1,230}?"
            r"\b(?:act|ordinance|rules|code|regulations?|constitution|order)\b(?:[,\s]+\d{4})?)",
            query[part.end():], re.I)
        if not match:
            continue
        name = match[1].strip()
        if (set(normalized(name).split()) - _GENERIC and
                not re.match(r"(?:section|article|rule|this|that|same)\b", name, re.I)):
            names.append(name)
            consumed = part.end() + match.end()

    # Direct/trailing mentions without preposition: "Section 19 Consumer Protection Act"
    for match in re.finditer(
        r"\b(?:the\s+)?((?:[a-zA-Z0-9\'-]+\s+){1,6}"
        r"(?:act|ordinance|rules|code|regulations?|constitution|order)\b(?:[,\s]+\d{4})?)",
        query, re.I
    ):
        if match.start() < consumed:
            continue
        raw_name = match[1].strip()
        if re.search(r"\b(?:this|that|same)\s+(?:act|ordinance|rules|code)\b", raw_name, re.I):
            continue
        # Strip leading question words or reference words
        words = raw_name.split()
        while words and (words[0].casefold() in _QUESTION_WORDS or re.match(r"^(?:sections?|sec\.?|s\.|rules?|articles?|art\.?|\d+)$", words[0], re.I)):
            words.pop(0)
        name = " ".join(words)
        cleaned_tokens = set(normalized(name).split()) - _GENERIC
        if cleaned_tokens and not re.match(r"^(?:section|article|rule|this|that|same)\b", name, re.I):
            names.append(name)

    return tuple(dict.fromkeys(names))


@dataclass
class PreparedQuery:
    text: str
    context_used: bool
    followup: bool
    references: tuple[LegalReferenceIn, ...]
    unsupported_reference: bool
    alternative: str | None


def prepare_query(request: LegalSearchIn):
    question = normalize_query(request.query)
    followup = request._explicit_followup or bool(_FOLLOWUP.search(question))
    previous = request.context.previous_query if request.context else None
    refs, unsupported = ([request.reference], False) if request.reference else _parse_references(question)
    context_used = bool(followup and previous and (not refs or request._explicit_followup))
    text = f"{normalize_query(previous)}\n{question}" if context_used else question
    if not refs and context_used and _REFERENCE_FOLLOWUP.search(question):
        prior, prior_unsupported = _parse_references(previous)
        if len(prior) == 1 and not prior_unsupported:
            refs = prior
    return PreparedQuery(text, context_used, followup, tuple(refs), unsupported,
                         query_alternative(text) if not refs else None)
