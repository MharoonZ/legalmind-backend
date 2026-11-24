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
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.qa.context import EvidenceUnit
from app.retrieval.references import parse_references, schedule_number
from app.retrieval.snapshot import Chunk, PublicationSnapshot, RootKey, Segment
from app.retrieval.text import STOPWORDS, compact_label, footnote_spans, stem, without_footnote_marks, words
from app.schemas.qa import VerifiedCitation, VerifiedClaim

_MARKER = re.compile(r"\[\s*((?:E\s?\d+)(?:\s*[,;&]\s*(?:E\s?\d+))*)\s*\]", re.I)
_KEY = re.compile(r"E\s?(\d+)", re.I)
_ELLIPSIS = re.compile(r"\.{3,}|…")
# PDF text wraps mid-sentence, so only sentence punctuation ends a sentence.
_SENTENCE = re.compile(r".+?(?:[.;](?=\s|$)|\.\u2013|$)", re.S)
_SPACE_BEFORE_PUNCTUATION = re.compile(r"(?<=\S)[ \t]+([.,;:!?])")
_PLACEHOLDER = "\x00{}\x01"
_PLACED = re.compile(r"\x00(\d+)\x01")
# Placeholders are made of control characters, so the model's text may not contain any.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# "[2]" written for "[E2]", on its own or straight after another marker.
_BARE_MARKERS = re.compile(rf"(?P<lead>(?i:{_MARKER.pattern}))?(?P<bare>(?:\[\d{{1,2}}\])+)")
# A citation mark in either form: the model's "[E1]" or the placeholder that replaces it.
_MARK = rf"(?:{_PLACED.pattern}|(?i:{_MARKER.pattern}))"
_CITED = re.compile(_MARK)
# Where one sentence or list item of an answer ends and the next begins: at a line
# break or after sentence punctuation. A capital must follow a Latin full stop, as
# the one in "Rs. 500" or "s. 5" ends nothing. Marks written after the full stop
# ("... one year. [E1] Next ...") stay with the sentence they follow.
_AFTER_STOP = rf"(?:[\"\u201d\u2019)\]]|[ \t]*(?:(?:and|&|[,;])[ \t]*)?{_MARK})*[ \t]+(?!{_MARK})"
_ANSWER_BREAK = re.compile(
    rf"(?<=[.!?]){_AFTER_STOP}(?=[\"\u201c\u2018(\[]?[A-Z])"
    rf"|(?<=\u06d4){_AFTER_STOP}"
    r"|[ \t]*\n+[ \t]*")
# The full stop after one of these does not end a sentence: "Act No. II of 2005", "Rs. 500".
_ABBREVIATIONS = frozenset(
    "no nos rs s ss sec secs art arts cl para paras sch ch pt reg r rr v vs dr mr mrs ms etc viz cf "
    "i.e e.g u/s".split())
# What numbers the items of a list: "1.", "12.", "a.", "iv.".
_LABEL = r"(?:\d{1,3}(?:\.\d{1,3})*|[a-z]|(?=[ivx])x{0,3}(?:ix|iv|v?i{0,3}))"
_LIST_LABEL = re.compile(_LABEL, re.I)
# The bullet and label that open a list item ("- ", "1. ", "(a) "): not part of what the item says.
_ITEM_START = re.compile(rf"[\s*_\u2022\u2013\u2014-]*(?:\(?{_LABEL}[.)\u06d4][*_]*(?=\s|$)\s*)?", re.I)
_LABEL_CONTEXT = 200         # characters before a full stop that tell a label or abbreviation from an ending
_BULLETS = "-*\u2022\u2013\u2014>#\t "
_OPENING = "([{\"'\u201c\u2018*_"
_CLOSING = ")]}\"'\u201d\u2019*_"
# "...other laws:- Under Section 2": a list the model wrote without line breaks.
# A statute's "namely:- (a) ..." is not one, so "(" may not follow.
_INLINE_BULLET = re.compile(r"(?<=[:.\]])[ \t]*-[ \t]+(?=[A-Z\[\"\u201c])")
_SPACE = re.compile(r"\s+")
_UNSPACED = re.compile(r"\S+")
# English number words, as statutes spell out terms and amounts ("five thousand rupees").
_NUMBER_WORDS = dict(zip(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
    "hundred thousand lakh crore".split(),
    [*range(1, 21), *range(30, 100, 10), 100, 1000, 100_000, 10_000_000]))
_ORDINALS = dict(zip("first second third fourth fifth sixth seventh eighth ninth tenth".split(), range(1, 11)))
# "5,000" and "1,00,000" are one number each; "sections 18,19,20" are three.
_GROUPED_NUMBER = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{2,3})*,\d{3}(?![\d,])")
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


def _ascii_digits(text: str) -> str:
    """Digits of every script as ASCII digits, so that a number in Urdu digits equals the same number in ours."""
    return text if text.isascii() else "".join(str(unicodedata.decimal(char, char)) for char in text)


def _spelt_numbers(tokens: Sequence[str]) -> set[str]:
    """Digits for the numbers ``tokens`` spell out in English: "five thousand" gives 5, 1000 and 5000."""
    found: set[str] = set()
    amount = part = 0               # of "five thousand two hundred": 5000 and 200
    for word in (*tokens, ""):
        value = _NUMBER_WORDS.get(word)
        # A number word continues the amount being read unless it starts another one ("one, two").
        continues = value is not None and (
            value >= 100 or part % 100 == 0 or (value < 10 and part % 100 in range(20, 100, 10)))
        if not continues:
            if amount + part:
                found.add(str(amount + part))
            amount = part = 0
        if value is None:
            if word in _ORDINALS:
                found.add(str(_ORDINALS[word]))
            continue
        found.add(str(value))
        if value >= 1000:
            amount, part = amount + max(part, 1) * value, 0
        elif value == 100:
            part = max(part, 1) * 100
        else:
            part += value
    return found


def _stems(text: str, *, source: bool = False) -> set[str]:
    """Word stems and numbers of ``text``, for comparing a statement with source wording.

    Numbers count as ASCII digits in whatever script or grouping they are written. ``source``
    text is English, where terms and amounts are usually spelt out: there the number words
    count as digits too, so that "3 years" matches "three years", and the footnote number
    of an amendment ("1[three years]") does not count at all.
    """
    if source:
        text = without_footnote_marks(text)
    tokens = words(_GROUPED_NUMBER.sub(lambda match: match[0].replace(",", ""), _ascii_digits(text)))
    found = {stem(word) for word in tokens if word not in STOPWORDS}
    return found | _spelt_numbers(tokens) if source else found


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


def _capped(text: str, start: int, end: int, wanted: set[str]) -> tuple[int, int] | None:
    """``text[start:end]``, or the part of it to quote when it is longer than a fallback quote may be.

    A long sentence is quoted where it supports the statement: the run of whole words, as
    long as the limit allows, that shares most with ``wanted``, and the earliest such run.
    """
    if end - start <= _MAX_FALLBACK_CHARS:
        return start, end
    pieces = [match.span() for match in _UNSPACED.finditer(text, start, end)]
    most = len(wanted & _stems(text[start:end], source=True))
    best, score, last = None, -1, 0
    for first, (low, _) in enumerate(pieces):
        last = max(last, first)
        while last + 1 < len(pieces) and pieces[last + 1][1] - low <= _MAX_FALLBACK_CHARS:
            last += 1
        high = pieces[last][1]
        if high - low > _MAX_FALLBACK_CHARS:
            continue                # one unbroken run longer than the limit: nothing to cut it at
        value = len(wanted & _stems(text[low:high], source=True))
        if value > score:
            best, score = (low, high), value
        if score >= most or last + 1 == len(pieces):
            break                   # later runs cannot share more
    return best


def _support(chunk: Chunk, claim: str) -> tuple[tuple[int, int] | None, int]:
    """The sentence of a passage that best supports ``claim``, and how many words they share."""
    wanted = _stems(claim)
    best, first, score, shared = None, None, 0.0, 0
    for match in _SENTENCE.finditer(chunk.text, _wording_start(chunk)):
        sentence = match[0].strip()
        if len(sentence) < 20:
            continue
        start = match.start() + (len(match[0]) - len(match[0].lstrip()))
        first = first or (start, start + len(sentence))
        found = _stems(sentence, source=True)
        common = len(wanted & found)
        value = common / (len(found) ** 0.5 or 1.0)
        if value > score:
            best, score, shared = (start, start + len(sentence)), value, common
    best = best or first            # nothing in common: the opening sentence of the wording
    if best is None:
        return None, 0
    return _capped(chunk.text, *best, wanted), shared


def _sanitised(answer: str) -> str:
    """The model's text made safe to mark up: no control characters, and "[2]" read as "[E2]".

    A bare number in brackets is a citation only where it stands free. Statutes print
    amendments as "1[three years]" and "section 3[59]": brackets that touch the text
    before them are the law's own wording and are left alone.
    """
    def marker(match: re.Match) -> str:
        before = match.string[match.start() - 1:match.start()]
        if match["lead"] is None and before and (before.isalnum() or before in "[](){}"):
            return match[0]
        return (match["lead"] or "") + _ascii_digits(match["bare"]).replace("[", "[E")
    return _BARE_MARKERS.sub(marker, _CONTROL.sub("", answer.replace("\r\n", "\n").replace("\r", "\n")))


def without_citations(answer: str) -> str:
    """Remove model citation tokens while preserving statutory bracketed wording."""
    text = _MARKER.sub("", _sanitised(answer))
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text).strip()


def _abbreviated(token: str) -> bool:
    """Whether a full stop after ``token`` belongs to it: "No.", "Rs.", "e.g.", or an initial as in "A. Khan"."""
    token = token.lstrip(_OPENING)
    return token.casefold() in _ABBREVIATIONS or (len(token) == 1 and token.isupper())


def _last_word(text: str) -> str:
    return text.rsplit(None, 1)[-1] if text.strip() else ""


def _ends_sentence(text: str, stop: int) -> bool:
    """Whether the punctuation at ``stop``, which a new sentence could follow, really ends one.

    The full stop of an abbreviation, of an initial or of a list label does not, so that a
    citation never lands inside "Act No. II of 2005" or behind the number of "1. Keeping ...".
    """
    if text[stop] not in ".\u06d4":
        return True
    near = max(stop - _LABEL_CONTEXT, 0)
    line = _CITED.sub("", text[max(text.rfind("\n", near, stop) + 1, near):stop]).rstrip()
    token = _last_word(line)
    if text[stop] == "." and _abbreviated(token):       # the Urdu full stop abbreviates nothing
        return False
    if not _LIST_LABEL.fullmatch(token.lstrip(_OPENING)):
        return True
    # "1", "a" or "iv" is a label only where a list item can begin: at the start of the line
    # or right after the previous item ("... keep records. 2. Issue receipts.").
    head = line[:len(line) - len(token)].rstrip().rstrip(_CLOSING)
    if not head.strip(_BULLETS) or head[-1] in ":;!?\u06d4":
        return False
    return head[-1] != "." or _abbreviated(_last_word(head[:-1]))


def _answer_sentences(text: str) -> list[tuple[int, int]]:
    """Character ranges of the sentences and list items of an answer.

    Citation marks written after a full stop lie between two ranges, and no range consists
    of marks alone: see ``_owner`` for the sentence such a mark belongs to.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _ANSWER_BREAK.finditer(text):
        if "\n" not in match[0] and not _ends_sentence(text, match.start() - 1):
            continue
        if _CITED.sub("", text[cursor:match.start()]).strip():
            spans.append((cursor, match.start()))
        cursor = match.end()
    if _CITED.sub("", text[cursor:]).strip():
        spans.append((cursor, len(text)))
    return spans


def _owner(sentences: Sequence[tuple[int, int]], position: int) -> int:
    """Index of the sentence that a citation mark at ``position`` supports.

    That is the sentence the mark stands in. A mark written after the full stop
    ("... one year. [E1] Next ...") stands between two sentences and supports the first.
    """
    return max(bisect_right(sentences, position, key=lambda span: span[0]) - 1, 0)


def _said(sentence: str) -> str:
    """What a sentence of the answer says: its text without citation marks, bullet or list label."""
    text = _CITED.sub("", sentence)
    return text[_ITEM_START.match(text).end():]


def _states(said: str) -> bool:
    """False for a heading ("Penalties:", "## Penalties") or an empty list item: nothing there needs a source."""
    said = said.strip(" \t*_")
    return any(map(str.isalpha, said)) and not said.endswith(":") and not said.startswith("#")


def _with_markers(text: str, additions: dict[tuple[int, int], list[int]]) -> str:
    """Append citation placeholders to the given sentences, before their final punctuation."""
    for start, end in sorted(additions, reverse=True):
        body = text[start:end].rstrip()
        # The full stop of a bare list label belongs to the label: nothing goes inside "1.".
        tail = body[-1] if body[-1:] in ".;:!?\u06d4" and any(map(str.isalnum, _said(body))) else ""
        core = body[:-1] if tail else body
        marks = "".join(_PLACEHOLDER.format(index) for index in dict.fromkeys(additions[(start, end)]))
        text = text[:start] + f"{core} {marks}{tail}" + text[start + len(body):]
    return text


def _attribute(answer: str, quotes: Sequence[str], unmarked: Sequence[int]) -> str:
    """Attach a citation placeholder to the answer sentence each quote supports.

    Used only when the model listed quotes but wrote no markers in its text,
    so every statement still points at its source. A quote goes to the sentence
    it shares most words and numbers with. One that shares nothing (the answer is
    in another language, or paraphrases freely) is placed by its position: quotes
    are listed in the order of the statements they support, so these go, in order,
    to the statements still without a source, and any left over to the last one.
    """
    pieces = _answer_sentences(answer)
    if not pieces:
        return answer
    said = [_said(answer[start:end]) for start, end in pieces]
    statements = [item for item, text in enumerate(said) if _states(text)] or list(range(len(pieces)))
    # Keep explicit attributions. Surplus model quotes have no place in a
    # response when every statement already identifies its source.
    occupied = {_owner(pieces, m.start()) for m in _PLACED.finditer(answer)}
    statements = [item for item in statements if item not in occupied]
    if not statements:
        return answer
    stems = {item: _stems(said[item]) for item in statements}
    additions: dict[tuple[int, int], list[int]] = {}
    unplaced: list[int] = []
    for index in unmarked:
        quote = quotes[index]
        wanted = _stems(quote, source=True)
        scores = {item: len(wanted & found) / (len(found) ** 0.5 or 1.0) for item, found in stems.items()}
        best = max(statements, key=scores.__getitem__)
        if scores[best] > 0:
            additions.setdefault(pieces[best], []).append(index)
        else:
            unplaced.append(index)
    uncited = [item for item in statements if pieces[item] not in additions]
    for position, index in enumerate(unplaced):
        best = uncited[position] if position < len(uncited) else statements[-1]
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
    answer = _sanitised(answer)
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
    headings: dict[str, tuple[EvidenceUnit, int, int]] = {}     # quotes of a heading only
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
        if end <= _wording_start(chunk) < len(chunk.text):
            # The provision's title is not what the law provides: prefer its
            # wording, and keep the title only if nothing better turns up.
            headings.setdefault(unit.key, (unit, start, end))
        elif cite(unit, start, end) is None:
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
    quoted = set(per_unit)          # passages with a usable quote from the model
    marked: set[int] = set()        # citations a marker has referred to so far
    settled: dict[tuple[int, str], int | None] = {}     # what a passage's markers cite in one sentence

    def numbers_for(match: re.Match) -> list[int]:
        nonlocal fallbacks
        found: list[int] = []
        # Every marker of a sentence supports that sentence, however the markers are spaced or joined.
        item = _owner(sentences, match.start())
        for key_match in _KEY.finditer(match[1]):
            unit = by_key.get(f"E{key_match[1]}")
            if unit is None:
                continue
            if (item, unit.key) in settled:
                index = settled[item, unit.key]
            elif unit.key in quoted:
                index = _closest(per_unit[unit.key], said[item])
            else:
                # No usable quote for this passage: cite the sentence of it that
                # supports each statement, so every marker points at relevant text.
                located, _ = _support(unit.chunk, said[item])
                known = len(citations)
                index = cite(unit, *located) if located is not None else None
                fallbacks += len(citations) - known
            settled[item, unit.key] = index
            if index is not None:
                found.append(index)
        marked.update(found)
        return list(dict.fromkeys(found))

    def _closest(indexes: list[int], sentence: str) -> int:
        """Of several quotes from one passage, the one this sentence is about."""
        if len(indexes) == 1:
            return indexes[0]
        wanted = _stems(sentence)
        shared = {index: len(wanted & _stems(citations[index].quote, source=True)) for index in indexes}
        best = max(indexes, key=lambda index: (shared[index], -index))
        if not shared[best]:
            # Nothing in common, as when the answer is not in English: the quotes of a
            # passage are listed in the order of its statements, so take the next unused one.
            best = next((index for index in indexes if index not in marked), best)
        return best

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
    if len(_INLINE_BULLET.findall(answer)) > 1:     # one alone is a quoted heading: "Penalty.- Whoever ..."
        answer = _INLINE_BULLET.sub("\n- ", answer)
    sentences = _answer_sentences(answer)
    said = [_said(answer[start:end]) for start, end in sentences] or [""]
    text = _MARKER.sub(lambda match: "".join(_PLACEHOLDER.format(index) for index in numbers_for(match)), answer)
    for unit, start, end in headings.values():
        if unit.key not in per_unit:            # still uncited: its wording, else the title itself
            located, shared = _support(unit.chunk, answer)
            if located is not None and shared and cite(unit, *located) is not None:
                fallbacks += 1
            elif cite(unit, start, end) is not None:
                verified += 1
            else:
                dropped += 1
    unmarked = [index for index in range(len(citations)) if index not in marked]
    if unmarked:
        text = _attribute(text, [citation.quote for citation in citations], unmarked)
    text = name_links(text)
    order: list[int] = list(dict.fromkeys(int(match[1]) for match in _PLACED.finditer(text)))
    position = {old: new for new, old in enumerate(order)}
    text = _PLACED.sub(lambda match: f"[{position[int(match[1])] + 1}]", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    # Spaces that open a line are the indentation of a nested list item and stay.
    text = re.sub(r"(?<=\S)[ \t]{2,}", " ", text).strip()
    ordered = tuple(citations[index] for index in order)
    claims = tuple(VerifiedClaim(text=citation.quote, evidence_id=citation.evidence_id,
                                 citation_indexes=(index,)) for index, citation in enumerate(ordered))
    return GroundedAnswer(text, claims, ordered, verified, dropped, fallbacks)
