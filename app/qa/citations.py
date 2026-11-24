"""Stage 16: tie the model's answer to exact source text and PDF pages.

The model cites evidence ids and copies short quotes. Here every quote is
located in the published text it claims to come from, tolerant of whitespace,
case, quotation-mark and dash glyphs, PDF footnote numbers and small copying
slips, and mapped through the stored source spans to a provision and a page.
A quote that cannot be found is dropped, not the whole answer; a cited passage
without a usable quote is cited through its best-matching sentence instead.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.qa.context import EvidenceUnit
from app.retrieval.snapshot import Chunk, PublicationSnapshot, Segment
from app.retrieval.text import STOPWORDS, footnote_spans, stem, words
from app.schemas.qa import VerifiedCitation, VerifiedClaim

_MARKER = re.compile(r"\[\s*((?:E\s?\d+)(?:\s*[,;&]\s*(?:E\s?\d+))*)\s*\]", re.I)
_KEY = re.compile(r"E\s?(\d+)", re.I)
_ELLIPSIS = re.compile(r"\.{3,}|…")
# PDF text wraps mid-sentence, so only sentence punctuation ends a sentence.
_SENTENCE = re.compile(r".+?(?:[.;](?=\s|$)|\.\u2013|$)", re.S)
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([.,;:!?])")
_PLACEHOLDER = "\x00{}\x01"
_PLACED = re.compile(r"\x00(\d+)\x01")
_ANSWER_SENTENCE = re.compile(r"[^.\n]+(?:\.+|\n+|$)")
# "...other laws:- Under Section 2": a list the model wrote without line breaks.
_INLINE_BULLET = re.compile(r"(?<=[:.\]])[ \t]*-[ \t]+(?=[A-Z(\[\"\u201c])")
_SPACE = re.compile(r"\s+")
_MIN_QUOTE = 12              # normalised characters
_FUZZY_CUTOFF = 88
_MAX_FALLBACK_CHARS = 320


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    answer: str
    claims: tuple[VerifiedClaim, ...]
    citations: tuple[VerifiedCitation, ...]
    verified_quotes: int
    dropped_quotes: int
    fallback_citations: int


def _normalized(text: str) -> tuple[str, list[int]]:
    """Lower-case words joined by single spaces, with each character's source offset."""
    skipped = {index for span in footnote_spans(text) for index in range(*span)}
    characters: list[str] = []
    offsets: list[int] = []
    gap = False
    for index, raw in enumerate(text):
        if index in skipped:
            continue
        for char in unicodedata.normalize("NFKC", raw).casefold():
            if char.isalnum():
                if gap and characters:
                    characters.append(" ")
                    offsets.append(index)
                gap = False
                characters.append(char)
                offsets.append(index)
            else:
                gap = True
    return "".join(characters), offsets


def _find_words(haystack: str, needle: str) -> int:
    """Index of ``needle`` in ``haystack`` aligned to word boundaries, or -1."""
    start = 0
    while (index := haystack.find(needle, start)) >= 0:
        end = index + len(needle)
        if (index == 0 or haystack[index - 1] == " ") and (end == len(haystack) or haystack[end] == " "):
            return index
        start = index + 1
    return -1


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Character range of ``quote`` inside ``text``, or None when it is not there."""
    source, offsets = _normalized(text)
    if not source:
        return None
    for piece in sorted(_ELLIPSIS.split(quote), key=len, reverse=True):
        wanted, _ = _normalized(piece)
        if len(wanted) < _MIN_QUOTE:
            continue
        index = _find_words(source, wanted)
        end = index + len(wanted)
        if index < 0:
            alignment = fuzz.partial_ratio_alignment(wanted, source, score_cutoff=_FUZZY_CUTOFF)
            if alignment is None or alignment.dest_end - alignment.dest_start < _MIN_QUOTE:
                continue
            index, end = alignment.dest_start, alignment.dest_end
            while index > 0 and source[index - 1] != " ":       # snap to whole words
                index -= 1
            while end < len(source) and source[end] != " ":
                end += 1
        return _whole_brackets(text, offsets[index], offsets[end - 1] + 1)
    return None


def _whole_brackets(text: str, start: int, end: int) -> tuple[int, int]:
    """Do not cut a quotation in the middle of "1[three years]" or a quoted term."""
    while end < len(text) and text[end] in "]\u201d\"\u2019":
        end += 1
    while start > 0 and text[start - 1] in "[\u201c\"\u2018":
        start -= 1
        if text[start] == "[":
            while start > 0 and text[start - 1].isdigit():
                start -= 1
    return start, end


def _anchor(chunk: Chunk, start: int, end: int) -> Segment | None:
    """The source span that carries most of the quoted characters."""
    best, overlap = None, 0
    for segment in chunk.segments:
        shared = min(end, segment.end) - max(start, segment.start)
        if shared > overlap:
            best, overlap = segment, shared
    return best


def _best_sentence(chunk: Chunk, claim: str) -> tuple[int, int] | None:
    """The sentence of a cited passage that best supports ``claim``."""
    wanted = {stem(word) for word in words(claim) if word not in STOPWORDS}
    body_start = chunk.body_start
    best, score = None, 0.0
    for match in _SENTENCE.finditer(chunk.text, body_start):
        sentence = match[0].strip()
        if len(sentence) < 20:
            continue
        found = {stem(word) for word in words(sentence) if word not in STOPWORDS}
        value = len(wanted & found) / (len(found) ** 0.5 or 1.0)
        if value > score:
            start = match.start() + (len(match[0]) - len(match[0].lstrip()))
            best, score = (start, min(start + len(sentence), start + _MAX_FALLBACK_CHARS)), value
    if best is None:
        first = next((m for m in _SENTENCE.finditer(chunk.text, body_start) if len(m[0].strip()) >= 20), None)
        if first is None:
            return None
        start = first.start() + (len(first[0]) - len(first[0].lstrip()))
        best = (start, min(first.end(), start + _MAX_FALLBACK_CHARS))
    return best


def _attribute(answer: str, quotes: Sequence[str]) -> str:
    """Attach a citation placeholder to the answer sentence each quote supports.

    Used only when the model listed quotes but wrote no markers in its text,
    so every statement still points at its source.
    """
    pieces = [match for match in _ANSWER_SENTENCE.finditer(answer) if match[0].strip()]
    if not pieces:
        return answer
    stems = [{stem(word) for word in words(match[0]) if word not in STOPWORDS} for match in pieces]
    additions: dict[int, list[int]] = {}
    for index, quote in enumerate(quotes):
        wanted = {stem(word) for word in words(quote) if word not in STOPWORDS}
        scores = [len(wanted & found) / (len(found) ** 0.5 or 1.0) for found in stems]
        best = max(range(len(pieces)), key=lambda item: scores[item])
        if scores[best] > 0:
            additions.setdefault(best, []).append(index)
    for piece in sorted(additions, reverse=True):
        match = pieces[piece]
        body = match[0].rstrip()
        tail = body[-1] if body[-1:] in ".;:" else ""
        core = body[:-1] if tail else body
        marked = f"{core} " + "".join(_PLACEHOLDER.format(index) for index in additions[piece]) + tail
        answer = answer[:match.start()] + marked + match[0][len(body):] + answer[match.end():]
    return answer


def ground_answer(answer: str, draft_citations: Sequence[dict], units: Sequence[EvidenceUnit],
                  snapshot: PublicationSnapshot) -> GroundedAnswer:
    by_key = {unit.key.upper(): unit for unit in units}
    by_public = {unit.evidence_id: unit for unit in units}
    citations: list[VerifiedCitation] = []
    spans: list[tuple[str, int, int]] = []          # (key, start, end) per citation
    per_unit: dict[str, list[int]] = {}
    verified = dropped = fallbacks = 0

    def cite(unit: EvidenceUnit, start: int, end: int) -> int | None:
        chunk = unit.chunk
        for index, (key, known_start, known_end) in enumerate(spans):
            if key == unit.key and known_start <= start and end <= known_end:
                return index
        anchor = _anchor(chunk, start, end)
        if anchor is None:
            return None
        law = snapshot.instruments[chunk.instrument_id]
        citations.append(VerifiedCitation(
            evidence_id=unit.evidence_id, publication_id=snapshot.id,
            legal_version_id=law.legal_version_id, revision_id=law.revision_id,
            provision_id=anchor.provision_id, chunk_id=chunk.id, law=law.title,
            edition=law.edition_key, jurisdiction=law.jurisdiction_code, kind=chunk.kind,
            label=chunk.label, reference_path=anchor.reference_path or chunk.reference_path,
            quote=_SPACE.sub(" ", chunk.text[start:end]).strip(),
            source_span_id=anchor.source_span_id, file_id=anchor.file_id,
            file_sha256=anchor.file_sha256, filename=anchor.filename,
            page_index=anchor.page_index, source_url=anchor.source_url))
        spans.append((unit.key, start, end))
        per_unit.setdefault(unit.key, []).append(len(citations) - 1)
        return len(citations) - 1

    for item in draft_citations:
        raw = str(item.get("evidence_id", "")).strip().strip("[]()")
        match = _KEY.fullmatch(raw)
        unit = by_key.get(f"E{match[1]}") if match else by_public.get(raw)
        if unit is None:
            dropped += 1
            continue
        chunk = unit.chunk
        located = locate_quote(chunk.text[chunk.body_start:], str(item.get("quote", "")))
        if located is None:
            dropped += 1
            continue
        start, end = located[0] + chunk.body_start, located[1] + chunk.body_start
        if cite(unit, start, end) is None:
            dropped += 1
        else:
            verified += 1

    def numbers_for(match: re.Match) -> list[int]:
        nonlocal fallbacks
        found: list[int] = []
        for key_match in _KEY.finditer(match[1]):
            unit = by_key.get(f"E{key_match[1]}")
            if unit is None:
                continue
            if unit.key not in per_unit:
                located = _best_sentence(unit.chunk, answer[sentence_bounds(match)[0]:match.start()])
                if located is None or cite(unit, *located) is None:
                    continue
                fallbacks += 1
            found.append(_closest(per_unit[unit.key], answer[sentence_bounds(match)[0]:match.start()]))
        return list(dict.fromkeys(found))

    def sentence_bounds(match: re.Match) -> tuple[int, int]:
        start = max(answer.rfind(mark, 0, match.start()) for mark in (". ", "\n", "] ")) + 1
        return start, match.start()

    def _closest(indexes: list[int], sentence: str) -> int:
        """Of several quotes from one passage, the one this sentence is about."""
        if len(indexes) == 1:
            return indexes[0]
        wanted = {stem(word) for word in words(sentence) if word not in STOPWORDS}
        return max(indexes, key=lambda index: (len(wanted & {
            stem(word) for word in words(citations[index].quote) if word not in STOPWORDS}), -index))

    # Markers become citation placeholders first; numbering then follows the
    # order in which citations are first used in the text.
    answer = _INLINE_BULLET.sub("\n- ", answer)
    text = _MARKER.sub(lambda match: "".join(_PLACEHOLDER.format(index) for index in numbers_for(match)), answer)
    if citations and not _PLACED.search(text):
        text = _attribute(text, [citation.quote for citation in citations])
    order: list[int] = list(dict.fromkeys(int(match[1]) for match in _PLACED.finditer(text)))
    order += [index for index in range(len(citations)) if index not in order]
    position = {old: new for new, old in enumerate(order)}
    text = _PLACED.sub(lambda match: f"[{position[int(match[1])] + 1}]", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    ordered = tuple(citations[index] for index in order)
    claims = tuple(VerifiedClaim(text=citation.quote, evidence_id=citation.evidence_id,
                                 citation_indexes=(index,)) for index, citation in enumerate(ordered))
    return GroundedAnswer(text, claims, ordered, verified, dropped, fallbacks)
