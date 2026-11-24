"""Stage 16: tie the model's answer to exact source text and PDF pages.

The model cites evidence ids and copies short quotes. Here every quote is
located in the published text it claims to come from, tolerant of whitespace,
case, quotation-mark and dash glyphs, PDF footnote numbers and small copying
slips, and mapped through the stored source spans to a provision and a page.
A quote that cannot be found is dropped, not the whole answer; a cited passage
without a usable quote is cited through its best-matching sentence instead, and
a provision the answer names without citing is linked to its text.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.qa.context import EvidenceUnit
from app.retrieval.references import parse_references, schedule_number
from app.retrieval.snapshot import Chunk, PublicationSnapshot, RootKey, Segment
from app.retrieval.text import STOPWORDS, compact_label, footnote_spans, stem, words
from app.schemas.qa import VerifiedCitation, VerifiedClaim

_MARKER = re.compile(r"\[\s*((?:E\s?\d+)(?:\s*[,;&]\s*(?:E\s?\d+))*)\s*\]", re.I)
_KEY = re.compile(r"E\s?(\d+)", re.I)
_ELLIPSIS = re.compile(r"\.{3,}|…")
# PDF text wraps mid-sentence, so only sentence punctuation ends a sentence.
_SENTENCE = re.compile(r".+?(?:[.;](?=\s|$)|\.\u2013|$)", re.S)
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([.,;:!?])")
_PLACEHOLDER = "\x00{}\x01"
_PLACED = re.compile(r"\x00(\d+)\x01")
# Where one sentence or list item of an answer ends and the next begins. A full
# stop inside "Rs. 500" or "s. 5" is not a break: a capital must follow.
_ANSWER_BREAK = re.compile(
    r"(?<=[.!?])(?:[\"\u201d\u2019)\]]|[ \t]*\x00\d+\x01)*[ \t]+(?=[\"\u201c\u2018(\[]?[A-Z])"
    r"|(?<=\u06d4)[ \t]+"
    r"|[ \t]*\n+[ \t]*")
# "...other laws:- Under Section 2": a list the model wrote without line breaks.
_INLINE_BULLET = re.compile(r"(?<=[:.\]])[ \t]*-[ \t]+(?=[A-Z(\[\"\u201c])")
_SPACE = re.compile(r"\s+")
_MIN_QUOTE = 12              # normalised characters
_FUZZY_CUTOFF = 88
_MAX_FALLBACK_CHARS = 320
# An unlocated quote is replaced by a source sentence only when that sentence
# shares at least this many of its words, and this share of them.
_MIN_SHARED_STEMS = 3
_MIN_SHARED_PART = 0.6


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


def _stems(text: str) -> set[str]:
    return {stem(word) for word in words(text) if word not in STOPWORDS}


def _wording_start(chunk: Chunk) -> int:
    """Where a provision's wording begins, after its printed number and heading."""
    start = chunk.body_start
    if chunk.heading:
        opening = chunk.text[start:start + len(chunk.heading) + 60]
        located = locate_quote(opening, chunk.heading)
        if located is not None:
            end = start + located[1]
            while end < len(chunk.text) and chunk.text[end] in " .:\u2013\u2014-\n\t":
                end += 1
            if end < len(chunk.text):
                return end
    return start


def _support(chunk: Chunk, claim: str) -> tuple[tuple[int, int] | None, int]:
    """The sentence of a passage that best supports ``claim``, and how many words they share."""
    wanted = _stems(claim)
    body_start = _wording_start(chunk)
    best, score, shared = None, 0.0, 0
    for match in _SENTENCE.finditer(chunk.text, body_start):
        sentence = match[0].strip()
        if len(sentence) < 20:
            continue
        found = _stems(sentence)
        common = len(wanted & found)
        value = common / (len(found) ** 0.5 or 1.0)
        if value > score:
            start = match.start() + (len(match[0]) - len(match[0].lstrip()))
            best, score, shared = (start, min(start + len(sentence), start + _MAX_FALLBACK_CHARS)), value, common
    if best is None:
        first = next((m for m in _SENTENCE.finditer(chunk.text, body_start) if len(m[0].strip()) >= 20), None)
        if first is None:
            return None, 0
        start = first.start() + (len(first[0]) - len(first[0].lstrip()))
        best = (start, min(first.end(), start + _MAX_FALLBACK_CHARS))
    return best, shared


def _answer_sentences(text: str) -> list[tuple[int, int]]:
    """Character ranges of the sentences and list items of an answer."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _ANSWER_BREAK.finditer(text):
        if text[cursor:match.start()].strip():
            spans.append((cursor, match.start()))
        cursor = match.end()
    if text[cursor:].strip():
        spans.append((cursor, len(text)))
    return spans


def _with_markers(text: str, additions: dict[tuple[int, int], list[int]]) -> str:
    """Append citation placeholders to the given sentences, before their final punctuation."""
    for start, end in sorted(additions, reverse=True):
        body = text[start:end].rstrip()
        tail = body[-1] if body[-1:] in ".;:!?\u06d4" else ""
        core = body[:-1] if tail else body
        marks = "".join(_PLACEHOLDER.format(index) for index in dict.fromkeys(additions[(start, end)]))
        text = text[:start] + f"{core} {marks}{tail}" + text[start + len(body):]
    return text


def _attribute(answer: str, quotes: Sequence[str]) -> str:
    """Attach a citation placeholder to the answer sentence each quote supports.

    Used only when the model listed quotes but wrote no markers in its text,
    so every statement still points at its source.
    """
    pieces = _answer_sentences(answer)
    if not pieces:
        return answer
    stems = [_stems(answer[start:end]) for start, end in pieces]
    additions: dict[tuple[int, int], list[int]] = {}
    for index, quote in enumerate(quotes):
        wanted = _stems(quote)
        scores = [len(wanted & found) / (len(found) ** 0.5 or 1.0) for found in stems]
        best = max(range(len(pieces)), key=lambda item: scores[item])
        if scores[best] > 0:
            additions.setdefault(pieces[best], []).append(index)
    return _with_markers(answer, additions)


def _named_roots(sentence: str, roots: Sequence[RootKey], snapshot: PublicationSnapshot) -> list[RootKey]:
    """Evidence provisions that ``sentence`` names by number ("Section 31 of the ... Act")."""
    parsed = parse_references(sentence)
    if not parsed:
        return []
    named = snapshot.laws.resolve(sentence, exclude=parsed.spans)
    if named.status == "unresolved":
        return []                               # a law outside the collection
    allowed = set(named.instrument_ids) if named.status in {"resolved", "ambiguous"} else None
    pool = [key for key in roots if allowed is None or key[0] in allowed]
    labels = {}
    for key in pool:
        first = snapshot.chunks_by_root[key][0]
        labels[key] = (first.kind, schedule_number(first.label) if first.kind == "schedule"
                       else compact_label(first.label))
    found: list[RootKey] = []
    for reference in parsed.references:
        same = [key for key in pool if labels[key] == (reference.kind, reference.label)]
        if not same and reference.kind != "schedule":      # "article 5" of an Act that has sections
            same = [key for key in pool if labels[key][0] != "schedule" and labels[key][1] == reference.label]
        if len(same) == 1:                      # the same number in two cited laws stays unlinked
            found.append(same[0])
    for item in parsed.ranges:
        holders = list(dict.fromkeys(key[0] for key in pool if labels[key][1] in {item.start, item.end}))
        if len(holders) == 1:
            between = set(snapshot.roots_between(holders[0], item.kind, item.start, item.end))
            found.extend(key for key in pool if key in between)
    return list(dict.fromkeys(found))


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

    unlocated: list[tuple[EvidenceUnit, str]] = []
    for item in draft_citations:
        raw = str(item.get("evidence_id", "")).strip().strip("[]()")
        match = _KEY.fullmatch(raw)
        unit = by_key.get(f"E{match[1]}") if match else by_public.get(raw)
        if unit is None:
            dropped += 1
            continue
        chunk = unit.chunk
        quote = str(item.get("quote", ""))
        located = locate_quote(chunk.text[chunk.body_start:], quote)
        if located is None:
            unlocated.append((unit, quote))
            continue
        start, end = located[0] + chunk.body_start, located[1] + chunk.body_start
        if cite(unit, start, end) is None:
            dropped += 1
        else:
            verified += 1
    # A paraphrased "quote" still says which passage the model relied on: cite
    # the sentence of that passage it restates, when there clearly is one.
    for unit, quote in unlocated:
        located, shared = (None, 0) if unit.key in per_unit else _support(unit.chunk, quote)
        needed = max(_MIN_SHARED_STEMS, math.ceil(_MIN_SHARED_PART * len(_stems(quote))))
        if located is None or shared < needed or cite(unit, *located) is None:
            dropped += 1
        else:
            fallbacks += 1

    def numbers_for(match: re.Match) -> list[int]:
        nonlocal fallbacks
        found: list[int] = []
        for key_match in _KEY.finditer(match[1]):
            unit = by_key.get(f"E{key_match[1]}")
            if unit is None:
                continue
            if unit.key not in per_unit:
                located, _ = _support(unit.chunk, answer[sentence_bounds(match)[0]:match.start()])
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

    def name_links(text: str) -> str:
        """Link each provision the answer names but never cites to that provision's text."""
        nonlocal fallbacks
        by_root: dict[RootKey, list[EvidenceUnit]] = {}
        for unit in units:
            by_root.setdefault((unit.chunk.instrument_id, unit.chunk.root_provision_id), []).append(unit)
        cited = {(by_public[citation.evidence_id].chunk.instrument_id,
                  by_public[citation.evidence_id].chunk.root_provision_id) for citation in citations}
        additions: dict[tuple[int, int], list[int]] = {}
        for start, end in _answer_sentences(text):
            sentence = _PLACED.sub("", text[start:end])
            for root in _named_roots(sentence, list(by_root), snapshot):
                if root in cited:
                    continue
                options = [(unit, *_support(unit.chunk, sentence)) for unit in by_root[root]]
                unit, located, _ = max(options, key=lambda option: option[2])
                index = cite(unit, *located) if located is not None else None
                if index is not None:
                    cited.add(root)
                    fallbacks += 1
                    additions.setdefault((start, end), []).append(index)
        return _with_markers(text, additions)

    # Markers become citation placeholders first; numbering then follows the
    # order in which citations are first used in the text.
    answer = _INLINE_BULLET.sub("\n- ", answer)
    text = _MARKER.sub(lambda match: "".join(_PLACEHOLDER.format(index) for index in numbers_for(match)), answer)
    if citations and not _PLACED.search(text):
        text = _attribute(text, [citation.quote for citation in citations])
    text = name_links(text)
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
