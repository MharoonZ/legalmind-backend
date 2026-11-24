"""Read numbered provision references out of free-form questions.

Tolerates abbreviations ("sec 3", "s. 3", "u/s 3"), misspelt keywords
("sction 3"), spelt-out numbers ("section three"), lists ("sections 3 and 4"),
ranges ("sections 3 to 5") and nested addresses ("sub-section (2) of section
5", "section 5(2)(a)"). Parsing never fails a request: anything it cannot read
is simply left to semantic search.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rapidfuzz.distance import OSA

from app.retrieval.text import compact_label, fold

Kind = Literal["section", "rule", "article", "schedule"]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}

_KIND_WORDS: dict[str, Kind] = {
    "section": "section", "sections": "section", "sec": "section", "secs": "section",
    "sect": "section", "s": "section", "ss": "section", "u/s": "section", "us": "section",
    "§": "section", "§§": "section", "dafa": "section", "dafaa": "section", "dafah": "section",
    "دفعہ": "section",
    "rule": "rule", "rules": "rule", "r": "rule",
    "article": "article", "articles": "article", "art": "article", "arts": "article",
    "schedule": "schedule", "schedules": "schedule", "sched": "schedule", "sch": "schedule",
}
_PLURAL = frozenset({"sections", "secs", "ss", "§§", "rules", "articles", "arts", "schedules"})
# Single letters and "us" are provision keywords only when a number follows.
_NEEDS_NUMBER = frozenset({"s", "ss", "r", "us"})
_FUZZY_KIND: tuple[tuple[str, Kind], ...] = (
    ("section", "section"), ("sections", "section"), ("article", "article"),
    ("articles", "article"), ("schedule", "schedule"),
)

_LABEL = r"\d{1,4}(?:-?[A-Za-z]{1,2})?(?![A-Za-z0-9])"
_CHILDREN = r"(?:\s?\(\s?[A-Za-z0-9]{1,4}\s?\))*"
_WORD_NUMBER = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_KIND_TOKEN = re.compile(r"u/s|§§?|دفعہ|[A-Za-z]+\.?", re.I)
_ADDRESS = re.compile(rf"\s*(?P<label>{_LABEL})(?P<children>{_CHILDREN})")
_SPELT = re.compile(rf"\s*(?P<word>{_WORD_NUMBER})\b", re.I)
_LIST = re.compile(rf"\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)(?P<label>{_LABEL})(?P<children>{_CHILDREN})", re.I)
_LIST_OR = re.compile(rf"\s*or\s+(?P<label>{_LABEL})(?P<children>{_CHILDREN})", re.I)
_RANGE = re.compile(rf"\s*(?:to|through|thru|till|until|upto|-|–|—)\s*(?P<label>{_LABEL})", re.I)
_UNIT = re.compile(r"\s*(?:years?|months?|days?|weeks?|hours?|rupees?|rs\b|persons?|times|percent|%|lakh|crore)", re.I)
_NESTED = re.compile(
    r"(?:sub-?\s?sections?|sub-?\s?rules?|clauses?|sub-?\s?clauses?|paragraphs?|paras?\.?)\s*"
    r"\(?\s?(?P<label>[A-Za-z0-9]{1,4})\s?\)?\s+(?:of|in|under)\s+(?:the\s+)?(?:said\s+)?$", re.I)
_SCHEDULE_BEFORE = re.compile(
    r"(?<![\w])(?P<ordinal>" + "|".join(_ORDINALS) + r")\s+schedule(?![\w])", re.I)
_SCHEDULE_AFTER = re.compile(
    r"(?<![\w])schedules?\s+(?P<label>\d{1,2}|[ivx]{1,4}|" + "|".join(_NUMBER_WORDS) + r")(?![\w])", re.I)
_SCHEDULE_BARE = re.compile(r"(?<![\w])the\s+schedule(?![\w])", re.I)


@dataclass(frozen=True, slots=True)
class Reference:
    kind: Kind
    label: str                         # compact root label: "3", "5a", "1" for the First Schedule
    children: tuple[str, ...] = ()     # ("2", "a") for section 3(2)(a)

    @property
    def display(self) -> str:
        if self.kind == "schedule":
            return f"Schedule {self.label}" if self.label else "the Schedule"
        suffix = "".join(f"({child})" for child in self.children)
        return f"{self.kind.capitalize()} {self.label.upper()}{suffix}"


@dataclass(frozen=True, slots=True)
class ReferenceRange:
    kind: Kind
    start: str
    end: str

    @property
    def display(self) -> str:
        return f"{self.kind.capitalize()}s {self.start.upper()} to {self.end.upper()}"


@dataclass(frozen=True, slots=True)
class ParsedReferences:
    references: tuple[Reference, ...] = ()
    ranges: tuple[ReferenceRange, ...] = ()
    spans: tuple[tuple[int, int], ...] = ()     # character ranges consumed in the text

    def __bool__(self) -> bool:
        return bool(self.references or self.ranges)


def schedule_number(label: str | None) -> str:
    """Schedules are printed as FIRST, I or 1; compare them as one number."""
    value = compact_label(label)
    if not value:
        return ""
    for table in (_ORDINALS, _NUMBER_WORDS, _ROMAN):
        if value in table:
            return str(table[value])
    return value


def _kind(word: str, *, number_follows: bool) -> tuple[Kind | None, bool]:
    key = fold(word).rstrip(".")
    if key in _KIND_WORDS:
        if key in _NEEDS_NUMBER and not number_follows:
            return None, False
        return _KIND_WORDS[key], key in _PLURAL
    if number_follows and len(key) >= 5 and key.isalpha():
        for target, kind in _FUZZY_KIND:
            limit = 2 if len(target) >= 7 else 1
            if abs(len(key) - len(target)) <= limit and OSA.distance(key, target, score_cutoff=limit) <= limit:
                return kind, target.endswith("s")
    return None, False


def _children(value: str) -> tuple[str, ...]:
    return tuple(fold(item) for item in re.findall(r"\(\s?([A-Za-z0-9]{1,4})\s?\)", value))


def parse_references(text: str) -> ParsedReferences:
    references: list[Reference] = []
    ranges: list[ReferenceRange] = []
    spans: list[tuple[int, int]] = []

    def covered(position: int) -> bool:
        return any(start <= position < end for start, end in spans)

    for match in _KIND_TOKEN.finditer(text):
        if covered(match.start()):
            continue
        if match.start() and (text[match.start() - 1].isalnum() or text[match.start() - 1] == "-"):
            continue                    # the tail of "sub-section" or of another word
        address = _ADDRESS.match(text, match.end())
        spelt = None if address else _SPELT.match(text, match.end())
        kind, plural = _kind(match[0], number_follows=bool(address or spelt))
        if kind is None or kind == "schedule" or not (address or spelt):
            continue
        if address and _UNIT.match(text, address.end()) and fold(match[0]).rstrip(".") in _NEEDS_NUMBER:
            continue                    # "s 3 years" is not a section
        end = (address or spelt).end()
        if address:
            label, children = compact_label(address["label"]), _children(address["children"])
        else:
            label, children = str(_NUMBER_WORDS[spelt["word"].lower()]), ()
        # "sub-section (2) of section 5" addresses a child of the root.
        prefix_children: list[str] = []
        start = match.start()
        while nested := _NESTED.search(text[max(0, start - 60):start]):
            prefix_children.insert(0, fold(nested["label"]))
            start = max(0, start - 60) + nested.start()
        if prefix_children and not children:
            children = tuple(reversed(prefix_children)) if len(prefix_children) > 1 else tuple(prefix_children)
        between = _RANGE.match(text, end) if address and not children else None
        if between and compact_label(between["label"]) != label:
            ranges.append(ReferenceRange(kind, label, compact_label(between["label"])))
            end = between.end()
        else:
            references.append(Reference(kind, label, children))
            while True:
                more = _LIST.match(text, end) or (plural and _LIST_OR.match(text, end))
                if not more or _UNIT.match(text, more.end()):
                    break
                references.append(Reference(kind, compact_label(more["label"]), _children(more["children"])))
                end = more.end()
        spans.append((start, end))

    for pattern, group in ((_SCHEDULE_BEFORE, "ordinal"), (_SCHEDULE_AFTER, "label")):
        for match in pattern.finditer(text):
            if not covered(match.start()):
                references.append(Reference("schedule", schedule_number(match[group])))
                spans.append(match.span())
    for match in _SCHEDULE_BARE.finditer(text):
        if not covered(match.start()):
            references.append(Reference("schedule", ""))
            spans.append(match.span())

    unique = tuple(dict.fromkeys(references))
    return ParsedReferences(unique, tuple(dict.fromkeys(ranges)), tuple(sorted(spans)))
