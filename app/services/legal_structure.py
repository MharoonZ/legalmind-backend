"""Conservative, deterministic draft parsing of stored legal-PDF blocks.

The parser never rewrites source text. Each non-whitespace character assigned to
a provision remains an exact slice of a stored FileBlock. Ambiguity is reported
for human review; this output is not an approved legal transcription.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.services.legal_status import (
    LEGAL_KINDS, direct_source_status, is_star_only, linked_note_status,
)

PARSER_VERSION = "legal-structure-v13"
NORMALIZER_VERSION = "legal-pages-v1"


@dataclass(frozen=True, slots=True)
class SourceBlock:
    id: UUID
    page_id: UUID
    extraction_id: UUID
    file_id: UUID
    page_index: int
    ordinal: int
    kind: str
    text: str
    bbox: dict | None
    page_height: float | None


@dataclass(frozen=True, slots=True)
class SourcePage:
    id: UUID
    extraction_id: UUID
    file_id: UUID
    page_index: int
    outcome: str
    warnings: list[dict]
    height_pt: float | None
    blocks: tuple[SourceBlock, ...]


@dataclass(frozen=True, slots=True)
class SourceDocument:
    file_id: UUID
    extraction_id: UUID
    title: str
    instrument_type: str
    page_count: int
    pages: tuple[SourcePage, ...]


@dataclass(slots=True)
class DraftNode:
    id: UUID
    parent_id: UUID | None
    kind: str
    label: str | None
    reference_path: str
    ordinal: int
    heading: str | None
    own_text: str = ""
    source_status: str | None = None


@dataclass(frozen=True, slots=True)
class DraftSpan:
    provision_id: UUID
    extraction_id: UUID
    page_id: UUID
    block_id: UUID
    provision_char_start: int
    provision_char_end: int
    block_char_start: int
    block_char_end: int


@dataclass(frozen=True, slots=True)
class DraftReference:
    provision_id: UUID
    raw_reference: str


@dataclass(frozen=True, slots=True)
class DraftNoteLink:
    note_provision_id: UUID
    target_provision_id: UUID | None
    marker_label: str
    extraction_id: UUID
    page_id: UUID
    block_id: UUID
    block_char_start: int
    block_char_end: int


@dataclass(frozen=True, slots=True)
class BlockDecision:
    block_id: UUID
    extraction_id: UUID
    page_id: UUID
    status: str
    reason: str


@dataclass(slots=True)
class ParsedStructure:
    nodes: list[DraftNode] = field(default_factory=list)
    spans: list[DraftSpan] = field(default_factory=list)
    references: list[DraftReference] = field(default_factory=list)
    note_links: list[DraftNoteLink] = field(default_factory=list)
    decisions: list[BlockDecision] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        payload = {
            "parser": PARSER_VERSION,
            "nodes": [(n.reference_path, n.kind, n.label, n.heading, n.own_text, n.source_status)
                      for n in self.nodes],
            "spans": [(s.provision_char_start, s.provision_char_end,
                       str(s.block_id), s.block_char_start, s.block_char_end)
                      for s in self.spans],
            "decisions": [(str(d.block_id), d.status, d.reason) for d in self.decisions],
            "note_links": [(str(link.note_provision_id), str(link.target_provision_id),
                            link.marker_label, str(link.block_id),
                            link.block_char_start, link.block_char_end)
                           for link in self.note_links],
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()


_REFERENCE = re.compile(
    r"\b(?:sections?|rules?|articles?)\s+\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*"
    r"|\b(?:First|Second|Third|Fourth|Fifth|Sixth|[IVXLCDM]+)\s+Schedule\b"
    r"|\bSchedule\s+(?:[IVXLCDM]+|\d+[A-Za-z]*)\b", re.IGNORECASE,
)
_SOURCE_NOTE_LINE = re.compile(
    r"(?m)^[ \t]*(?P<label>\d{1,3})(?=[ \t]*[A-Z](?:[A-Za-z]|[ \t]+))"
)
_AMENDMENT_PREFIX = re.compile(r"^(?P<label>\d{1,3})\[(?=\(?[A-Za-z0-9])")
_INLINE_MARKER = re.compile(r"(?<![A-Za-z0-9])(?P<label>\d{1,3})(?=\[)")
_TITLE_MARKER = re.compile(r"^[ \t]*(?P<label>\d{1,3})(?=[A-Z]{2,})")
_YEAR_NOTE_MARKER = re.compile(r"\b(?:18|19|20)\d{2}(?P<label>[1-9])(?=[,.;:)\]])")
_LEGAL_NOUN_NOTE_MARKER = re.compile(
    r"(?<![A-Za-z0-9])(?:Act|Code|Ordinance|Rules?|Schedule|Order|Constitution)"
    r"(?P<label>[1-9])(?=[ \t,.;:)\]])", re.IGNORECASE,
)
_WORD_NOTE_MARKER = re.compile(r"\b[A-Za-z]{3,}(?P<label>[1-9])(?=[ \t,.;:)\]])")
_ROMAN_ITEM = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
               "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
_CONTENTS_HEADING = re.compile(
    r"^(?:C\s*O\s*N\s*T\s*E\s*N\s*T\s*S|TABLE OF CONTENTS)\s*$", re.IGNORECASE,
)
_LEGAL_LABEL = r"\d+(?:-[A-Za-z]+|[A-Za-z]*)(?:\([A-Za-z0-9]+\))*"
_SECTION = re.compile(rf"^(?:(?:Section|Sec\.)[ \t]+)?({_LEGAL_LABEL})\.[ \t]+", re.IGNORECASE)
_COMBINED_SECTION = re.compile(r"^(\d+[A-Z]*)[ \t]+and[ \t]+(\d+[A-Z]*)\.[ \t]+", re.IGNORECASE)
_EXPLICIT_SECTION = re.compile(rf"^(?:Section|Sec\.)[ \t]+({_LEGAL_LABEL})(?:[.:][ \t]+|[ \t]+[-–—][ \t]+)", re.IGNORECASE)
_EXPLICIT_RULE = re.compile(r"^Rule[ \t]+(\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*)[. :\-–—][ \t]*", re.IGNORECASE)
_EXPLICIT_ARTICLE = re.compile(r"^Article[ \t]+(\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*)[. :\-–—][ \t]*", re.IGNORECASE)
_PART = re.compile(r"^PART[ \t]+([IVXLCDM]+|\d+[A-Za-z]*)\b", re.IGNORECASE)
_CHAPTER = re.compile(r"^CHAPTER(?:[ \t]+|[-–—])([IVXLCDM]+|\d+[A-Za-z]*)\b", re.IGNORECASE)
_SCHEDULE = re.compile(
    r"^\[?(?:THE[ \t]+)?(?:(FIRST|SECOND|THIRD|FOURTH|FIFTH|[IVXLCDM]+)[ \t]+SCHEDULE"
    r"|SCHEDULE[ \t]+([IVXLCDM]+|\d+[A-Za-z]*))\b", re.IGNORECASE,
)
_UNNUMBERED_SCHEDULE = re.compile(
    r"^(?:THE[ \t]+)?SCHEDULE(?=[.:\-\[]|[ \t]*\]?[ \t]*$)",
)
_ROMAN_DIVISION = re.compile(r"^([IVXLCDM]+)[ \t]*(?:[-\u2013\u2014\ufffd]|â€“|â€”)[ \t]*([^\n]+)", re.IGNORECASE)
# A clause label is short; words such as (Punjab) and (inclusive) are prose.
_PAREN = re.compile(r"^\((\d{1,3}|[a-z]{1,3}|[A-Z])\)(?=[ \t]|[A-Za-z\u201c\"'])")
_INLINE_CHILD = re.compile(
    r"[.:\-\u2013\u2014\ufffd][ \t\n]*(?P<amendment>\d{1,3}\[)?"
    r"(?P<marker>\((?:\d{1,3}|[a-z]{1,3}|[A-Z])\))(?=[ \t\n.\]]|[A-Za-z\u201c\"'])"
)
_RUN_IN_SUBSECTION = re.compile(
    r"^(?P<caption>[A-Z][A-Za-z][^.!?\n]{0,90}?)\.[ \t]*"
    r"[-\u2013\u2014\ufffd][ \t]*\((?P<label>\d{1,3})\)"
    r"(?=[ \t\n]|[A-Za-z])"
)
_POST_LIST_MODAL = re.compile(
    r"\bshall\b(?=[^.;:]{0,120}\b(?:punish(?:ed|able)|liable|sentenced|fined|imprisoned)\b)",
    re.IGNORECASE,
)
_PREAMBLE = re.compile(r"^Preamble\b[. :\-–—]*", re.IGNORECASE)
_EXPLANATION = re.compile(r"^Explanations?\b[. :\-–—]*", re.IGNORECASE)
_MIXED_RUNNING_HEADER = re.compile(
    r"\A[ \t]*(?:\d{1,4}[ \t]*\n[ \t]*The Punjab Laws[ \t]*"
    r"|[^\n]{8,160}[ \t]*\n[ \t]*\d{1,4}[ \t]*)\n(?:[ \t]*\n)*",
    re.IGNORECASE,
)
_WRAPPED_REFERENCE = re.compile(
    r"\b(?:of|under|in|by)[ \t]+(?:sub[ \t-]*section|clause|section)[ \t\n]*$",
    re.IGNORECASE,
)
# "...an offence specified in the" / "First Schedule to this Act." and
# "...prescribed under" / "rule 5 of the Rules": the sentence is still open, so
# the next line continues it rather than starting a Schedule, Rule or Article.
_OPEN_SENTENCE = re.compile(
    r"\b(?:the|in|under|of|to|by|said|see|vide|with|per|any|such|that|this|following)[ \t\n]*$",
    re.IGNORECASE,
)


def _boundary(line: str, instrument_type: str) -> tuple[str, str | None, str | None] | None:
    candidate = line.lstrip(" \t")
    prefix = _AMENDMENT_PREFIX.match(candidate)
    if prefix:
        candidate = candidate[prefix.end():]
    if re.match(r"^but does not include\b", candidate, re.IGNORECASE):
        return "post_list_continuation", None, None
    if _POST_LIST_MODAL.match(candidate):
        return "post_list_continuation", None, None
    if re.match(r"^Provided that\b", candidate, re.IGNORECASE):
        return "proviso_continuation", None, None
    if _EXPLANATION.match(candidate):
        return "explanation", None, "Explanation"
    unnumbered_schedule = _UNNUMBERED_SCHEDULE.match(candidate)
    if unnumbered_schedule:
        return "schedule", None, _heading(candidate[unnumbered_schedule.end():])
    for kind, expression in (("part", _PART), ("chapter", _CHAPTER),
                             ("schedule", _SCHEDULE), ("preamble", _PREAMBLE),
                             ("rule", _EXPLICIT_RULE), ("article", _EXPLICIT_ARTICLE),
                             ("section", _EXPLICIT_SECTION)):
        match = expression.match(candidate)
        if match:
            label = next((group for group in match.groups() if group), None)
            return kind, label, "Preamble" if kind == "preamble" else _heading(candidate[match.end():])
    match = _ROMAN_DIVISION.match(candidate)
    if match:
        # Some Acts print I–PRELIMINARY instead of PART I or CHAPTER I.
        return "part", match.group(1), _heading(match.group(2))
    match = _COMBINED_SECTION.match(candidate)
    if match:
        # One jointly repealed source heading is not two invented provisions.
        return "section", f"{match.group(1)} and {match.group(2)}", _heading(candidate[match.end():])
    match = _SECTION.match(candidate)
    if match:
        return ("rule" if instrument_type == "rule" else "section",
                match.group(1), _heading(candidate[match.end():]))
    match = _RUN_IN_SUBSECTION.match(candidate)
    if match:
        return "run_in_subsection", match.group("label"), match.group("caption")
    match = _PAREN.match(candidate)
    if match:
        label = match.group(1)
        return ("subsection" if label.isdigit() else "paragraph", label,
                _heading(candidate[match.end():]))
    return None


def _heading(remainder: str) -> str | None:
    # The first source line often contains both heading and body separated by
    # an em dash or replacement character. Do not invent a cleaned heading.
    text = remainder.strip()
    if not text:
        return None
    text = re.split(r"[\ufffd—–]|\s+-\s+", text, maxsplit=1)[0].strip()
    return text[:240] or None


def _unfinished_section_heading_line(line: str) -> bool:
    """A numbered heading whose caption has wrapped before its full stop/dash."""
    candidate = line.lstrip()
    prefix = _AMENDMENT_PREFIX.match(candidate)
    if prefix:
        candidate = candidate[prefix.end():]
    match = _SECTION.match(candidate)
    if not match:
        return False
    tail = candidate[match.end():].strip()
    return (len(tail) < 240 and not re.search(r"[.!?\u2013\u2014\ufffd]", tail)
            and not re.search(r"\b(?:shall|must|may|means|includes)\b", tail, re.I))


def _completed_wrapped_section_heading(text: str) -> str | None:
    lines = text.splitlines()
    if len(lines) < 2 or not _unfinished_section_heading_line(lines[0]):
        return None
    candidate = text.lstrip()
    prefix = _AMENDMENT_PREFIX.match(candidate)
    if prefix:
        candidate = candidate[prefix.end():]
    match = _SECTION.match(candidate)
    if not match:
        return None
    caption = re.match(r"(?s)^(.{2,240}?)\.[ \t]*[-\u2013\u2014\ufffd]", candidate[match.end():])
    return " ".join(caption.group(1).split()) if caption else None


def _line_boundaries(text: str, instrument_type: str, initial_letter_items: int = 0,
                     initial_previous_line: str = "", continuation_lookahead: str = ""):
    def next_item_is_two(offset: int) -> bool:
        next_item = re.search(
            r"(?m)^[ \t]*\((\d{1,3}|[a-z]{1,3}|[A-Z])\)[ \t]+",
            text[offset:offset + 500],
        )
        return next_item is not None and next_item.group(1) == "2"

    detected = []
    previous_line = initial_previous_line
    letter_items = initial_letter_items
    for match in re.finditer(r"[^\n]+", text):
        value = _boundary(match.group(), instrument_type)
        if (value is not None and value[0] == "run_in_subsection"
                and value[1] == "1" and _unfinished_section_heading_line(previous_line)):
            # The caption completes a line-wrapped numbered heading, rather
            # than opening a sibling subsection of an earlier section. The
            # inline-child pass below will still open (1) at its exact offset.
            value = None
        if value is None and letter_items >= 2 and previous_line.rstrip().endswith((",", ";", ":")):
            # A sentence following a multi-item list can resume the parent
            # provision. Require both list context and consequence grammar;
            # indentation alone is unreliable in extracted PDFs.
            if re.match(r"^\s*the\s+(?:[A-Za-z][A-Za-z-]*\s+){1,5}may\b", match.group(), re.I):
                value = ("post_list_continuation", None, None)
        if value is not None:
            position = match.start() + len(match.group()) - len(match.group().lstrip(" \t"))
            if value[0] == "paragraph" and value[1] == "l" and next_item_is_two(match.end()):
                value = ("ambiguous_one", "l", value[2])
            detected.append((position, value))
            if value[0] in {"section", "rule", "article", "subsection", "run_in_subsection"}:
                letter_items = 0
            elif value[0] == "paragraph":
                letter_items += 1
        if match.group().strip():
            previous_line = match.group()
    # PDF text often wraps "shall ... be punished" across physical lines.
    # Detect the consequence over the whole block, then let the parent-stack
    # check decide whether it belongs to a list's parent or its last child.
    for modal in _POST_LIST_MODAL.finditer(text + continuation_lookahead[:240]):
        if modal.start() < len(text):
            detected.append((modal.start(), ("post_list_modal", None, None)))
    detected = sorted({position: value for position, value in detected}.items())
    for index, (position, value) in enumerate(detected):
        yield position, value
        if value[0] not in {"section", "rule", "article"}:
            continue
        # A first child may follow a multi-line heading and may itself carry
        # an amendment marker. Do not search beyond the next provision.
        limit = min(position + 500,
                    detected[index + 1][0] if index + 1 < len(detected) else len(text))
        inline = _INLINE_CHILD.search(text, position + 2, limit)
        if inline:
            child = _boundary(inline.group("marker") + " ", instrument_type)
            if child is not None and child[0] in {"subsection", "paragraph"}:
                if (child[0] == "paragraph" and child[1] == "l"
                        and next_item_is_two(limit)):
                    child = ("ambiguous_one", "l", child[2])
                yield inline.start("amendment") if inline.group("amendment") else inline.start("marker"), child


def _signature(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "", text.casefold())).strip()


def _body_opening_page(page: SourcePage, title: str) -> bool:
    """Distinguish a repeated body title from chapter/part entries in contents."""
    upper = [block.text for block in page.blocks[:5]
             if block.bbox and page.height_pt
             and float(block.bbox.get("y0", 1e9)) < page.height_pt * 0.26]
    printed = re.sub(r"(?m)^\s*\d{1,3}(?=THE\b)", "", " ".join(upper))
    title_words = set(re.findall(r"[a-z]{3,}", title.casefold()))
    printed_words = set(re.findall(r"[a-z]{3,}", printed.casefold()))
    if len(title_words & printed_words) < max(3, len(title_words) - 1):
        return False
    opening = " ".join(block.text for block in page.blocks[:8]).casefold()
    return bool(re.search(r"\ban act\b|\bpreamble\b|\bwhereas\b", opening))


def _contents_body_offset(text: str, title: str) -> int | None:
    """Find a body opening *within* a contents page/block, using corroborating signals.

    A repeated title by itself is not sufficient: contents pages often print it.
    The opening must also have an enactment/preamble and operative heading nearby.
    """
    title_words = set(re.findall(r"[a-z]{3,}", title.casefold()))
    lines = list(re.finditer(r"[^\n]+", text))
    for index, line in enumerate(lines):
        candidate = re.sub(r"^\s*\d{1,3}(?=[A-Z]{2,})", "", line.group()).strip()
        candidate_words = set(re.findall(r"[a-z]{3,}", candidate.casefold()))
        if len(title_words & candidate_words) < max(2, len(title_words) - 1):
            continue
        if len(candidate_words - title_words) > 2:
            continue
        following = "\n".join(item.group() for item in lines[index + 1:index + 12])
        if (re.search(r"\b(?:an act|an ordinance|preamble|whereas)\b", following, re.I)
                and re.search(r"(?m)^\s*(?:\d{1,3}\[)?\d+[A-Za-z-]*\.[ \t]+\S", following)):
            return line.start()
    # Some PDFs omit a repeated title; require both preamble and substantive
    # numbered provision text after it before leaving contents mode.
    for index, line in enumerate(lines):
        if not re.match(r"\s*(?:Preamble\b|WHEREAS\b)", line.group(), re.I):
            continue
        following = "\n".join(item.group() for item in lines[index + 1:index + 12])
        if re.search(r"(?m)^\s*(?:\d{1,3}\[)?\d+[A-Za-z-]*\.[ \t]+\S", following):
            return line.start()
    return None


def _is_header(block: SourceBlock, title: str, repeated: set[str],
               page_note_labels: set[str]) -> bool:
    if block.ordinal != 1 or not block.bbox or not block.page_height:
        return False
    if float(block.bbox.get("y0", 1e9)) > block.page_height * 0.16:
        return False
    text = block.text.strip()
    if len(text) > 160:
        return False
    title_marker = _TITLE_MARKER.match(text)
    if title_marker and title_marker.group("label") in page_note_labels:
        # A title-side amendment marker is an anchor for a note on this page,
        # not disposable running furniture.
        return False
    if any(_boundary(line, "act") for line in text.splitlines() if line.strip()):
        return False
    signature = _signature(text)
    title_words = set(re.findall(r"[a-z]{4,}", title.casefold()))
    overlap = len(title_words & set(re.findall(r"[a-z]{4,}", text.casefold())))
    return signature in repeated or (
        bool(re.search(r"(?:^\d+\b|\b\d+$)", text))
        and (overlap >= 2 or "laws" in text.casefold())
    )


def _mixed_running_header_end(block: SourceBlock, title: str) -> int:
    if block.ordinal != 1 or not block.bbox or not block.page_height:
        return 0
    if float(block.bbox.get("y0", 1e9)) > block.page_height * 0.16:
        return 0
    match = _MIXED_RUNNING_HEADER.match(block.text)
    if not match or not block.text[match.end():].strip():
        return 0
    header = block.text[:match.end()].casefold()
    if "the punjab laws" in header:
        return match.end()
    title_words = set(re.findall(r"[a-z]{3,}", title.casefold())) - {"the"}
    header_words = set(re.findall(r"[a-z]{3,}", header)) - {"the"}
    required_overlap = len(title_words) if len(title_words) <= 2 else len(title_words) - 1
    if title_words and len(title_words & header_words) >= required_overlap:
        return match.end()
    return 0


def _is_footnote(block: SourceBlock) -> bool:
    if block.kind in {"footnote", "note"}:
        return True
    return bool(block.page_height and block.bbox
                and float(block.bbox.get("y0", 0)) > block.page_height * 0.45
                and _SOURCE_NOTE_LINE.search(block.text))


def _is_wrapped_reference(block: SourceBlock, position: int,
                          previous: DraftNode | None) -> bool:
    preceding = block.text[:position].rstrip()
    if not preceding and previous is not None:
        preceding = previous.own_text.rstrip()
    return bool(_WRAPPED_REFERENCE.search(preceding[-100:]))


def _is_wrapped_named_reference(block: SourceBlock, position: int,
                                previous: DraftNode | None) -> bool:
    """A Schedule/Rule/Article name that only finishes the preceding sentence."""
    preceding = block.text[:position].rstrip()
    if not preceding and previous is not None:
        preceding = previous.own_text.rstrip()
    return bool(_OPEN_SENTENCE.search(preceding[-60:]))


def _continues_list(previous: str | None, incoming: str | None) -> bool:
    if not previous or not incoming:
        return False
    first, second = previous.lower(), incoming.lower()
    if first in _ROMAN_ITEM and second in _ROMAN_ITEM:
        if _ROMAN_ITEM[second] == _ROMAN_ITEM[first] + 1:
            return True
    return len(first) == len(second) == 1 and ord(second) == ord(first) + 1


def _open_parent_sentence(text: str) -> bool:
    """A list interrupted a conditional subject before its legal consequence."""
    matches = list(re.finditer(
        r"\b(?:whoever|whosoever|where|when|if|provided\s+that|(?:any|every|a)\s+person(?:\s+who)?)\b",
        text, re.IGNORECASE,
    ))
    if not matches:
        return False
    trailing = text[matches[-1].start():]
    return not re.search(r"\b(?:shall|must|may)\b", trailing, re.IGNORECASE)


def _isolated_heading_token(text: str, label: str | None) -> bool:
    return bool(label and re.fullmatch(
        rf"\s*(?:(?:Section|Sec\.|Rule|Article)\s+)?{re.escape(label)}\.\s*",
        text, re.IGNORECASE,
    ))


def parse_structure(documents: list[SourceDocument]) -> ParsedStructure:
    result = ParsedStructure()
    used_paths: dict[str, int] = {}
    mapped: dict[UUID, list[tuple[int, int]]] = {}
    intentional: dict[UUID, str] = {}
    current: DraftNode | None = None
    part: DraftNode | None = None
    chapter: DraftNode | None = None
    section: DraftNode | None = None
    subsection: DraftNode | None = None
    paragraph: DraftNode | None = None
    schedule: DraftNode | None = None
    list_parent: DraftNode | None = None
    last_list_label: str | None = None
    proviso_parent: DraftNode | None = None
    proviso_preceding_label: str | None = None
    counters: dict[str, int] = {}
    current_prefix = ""
    page_markers: list[tuple[str, SourceBlock, int, int, UUID | None]] = []
    page_notes: dict[str, DraftNode] = {}

    def flag(code: str, message: str, *, page: SourcePage | None = None,
             block: SourceBlock | None = None) -> None:
        result.warnings.append({"code": code, "message": message,
                                "extraction_id": str((block or page).extraction_id) if (block or page) else None,
                                "page_index": (block or page).page_index if (block or page) else None,
                                "block_id": str(block.id) if block else None})

    def make_node(kind: str, label: str | None, heading: str | None,
                  parent: DraftNode | None, block: SourceBlock) -> DraftNode:
        segment = {
            "preamble": "pre", "part": "part", "chapter": "ch", "section": "s",
            "subsection": "sub", "paragraph": "p", "rule": "r", "schedule": "sch",
            "article": "a", "other": "other",
        }[kind]
        if label is None:
            counters[segment] = counters.get(segment, 0) + 1
            label_key = str(counters[segment])
        else:
            label_key = label.upper() if kind in {"part", "chapter", "schedule"} else label
        root = parent.reference_path if parent else current_prefix
        if kind == "other" and label and heading == f"Source note {label}":
            root = f"{current_prefix}/page:{block.page_index}"
            segment = "note"
        base = f"{root}/{segment}:{label_key}"
        count = used_paths.get(base, 0) + 1
        used_paths[base] = count
        path = base if count == 1 else f"{base}/duplicate:{count}"
        if count > 1:
            flag("duplicate_identifier", f"Repeated legal identifier {base}; kept separately as {path}.", block=block)
        node = DraftNode(uuid4(), parent.id if parent else None, kind, label,
                         path, len(result.nodes) + 1, heading)
        result.nodes.append(node)
        if heading and len(heading) >= 240:
            flag("ambiguous_heading", "Heading exceeded the inspection limit; verify its boundary.", block=block)
        return node

    def append(node: DraftNode, block: SourceBlock, start: int, end: int,
               *, scan_markers: bool = True) -> None:
        text = block.text[start:end]
        if not text.strip():
            return
        separator = "\n" if node.own_text else ""
        output_start = len(node.own_text) + len(separator)
        node.own_text += separator + text
        result.spans.append(DraftSpan(node.id, block.extraction_id, block.page_id,
                                      block.id, output_start, output_start + len(text), start, end))
        mapped.setdefault(block.id, []).append((start, end))
        if scan_markers:
            for match in _INLINE_MARKER.finditer(text):
                page_markers.append((match.group("label"), block,
                                     start + match.start("label"),
                                     start + match.end("label"), node.id))
            for match in _YEAR_NOTE_MARKER.finditer(text):
                if match.group("label") in page_note_labels:
                    page_markers.append((match.group("label"), block,
                                         start + match.start("label"),
                                         start + match.end("label"), node.id))
            for match in _LEGAL_NOUN_NOTE_MARKER.finditer(text):
                if match.group("label") in page_note_labels:
                    page_markers.append((match.group("label"), block,
                                         start + match.start("label"),
                                         start + match.end("label"), node.id))
            for match in _WORD_NOTE_MARKER.finditer(text):
                if match.group("label") in page_note_labels:
                    page_markers.append((match.group("label"), block,
                                         start + match.start("label"),
                                         start + match.end("label"), node.id))
            if start == 0:
                title_marker = _TITLE_MARKER.match(text)
                if title_marker:
                    page_markers.append((title_marker.group("label"), block,
                                         title_marker.start("label"),
                                         title_marker.end("label"), node.id))

    for document in documents:
        current_prefix = f"f:{document.file_id}"
        current = part = chapter = section = subsection = paragraph = list_parent = schedule = None
        proviso_parent = proviso_preceding_label = None
        last_list_label = None
        counters = {}
        # Repetition is evidence of page furniture, never evidence of law.
        signatures: dict[str, int] = {}
        for page in document.pages:
            if page.blocks:
                first = page.blocks[0]
                if first.bbox and page.height_pt and float(first.bbox.get("y0", 1e9)) <= page.height_pt * 0.16:
                    key = _signature(first.text)
                    signatures[key] = signatures.get(key, 0) + 1
        repeated = {key for key, count in signatures.items() if count > 1 and key}
        actual_indexes = [page.page_index for page in document.pages]
        expected_indexes = list(range(1, document.page_count + 1))
        if actual_indexes != expected_indexes:
            flag("missing_pages", f"Expected physical pages 1..{document.page_count}; found {actual_indexes}.")
        in_contents = False
        for page in document.pages:
            if in_contents and _body_opening_page(page, document.title):
                in_contents = False
                current = part = chapter = section = subsection = paragraph = list_parent = schedule = None
                last_list_label = None
            in_footnotes = False
            current_note: DraftNode | None = None
            page_markers = []
            page_notes = {}
            page_note_labels: set[str] = set()
            in_page_note_region = False
            for note_block in page.blocks:
                in_page_note_region = in_page_note_region or _is_footnote(note_block)
                if in_page_note_region:
                    page_note_labels.update(match.group("label")
                                            for match in _SOURCE_NOTE_LINE.finditer(note_block.text))
            if page.outcome not in {"extracted", "verified_blank"}:
                flag("source_page_unreviewed", f"Page {page.page_index} outcome is {page.outcome}.", page=page)
            positioned = [block for block in page.blocks if block.kind != "figure" and block.bbox]
            for index, left in enumerate(positioned):
                for right in positioned[index + 1:]:
                    a, b = left.bbox, right.bbox
                    vertical_overlap = min(float(a["y1"]), float(b["y1"])) - max(float(a["y0"]), float(b["y0"]))
                    if vertical_overlap > 20 and (float(a["x1"]) + 20 < float(b["x0"]) or
                                                  float(b["x1"]) + 20 < float(a["x0"])):
                        flag("possible_multi_column", "Side-by-side text blocks may have an ambiguous reading order.", page=page)
                        break
                else:
                    continue
                break
            for page_warning in page.warnings:
                if page_warning.get("severity") == "error" or page_warning.get("code") in {"blank_candidate", "ocr_unverified", "mixed_reading_order"}:
                    flag("source_extraction_warning", str(page_warning.get("message", page_warning)), page=page)
            if current and current.kind == "other" and current.heading == "Contents":
                current = None
            for block_index, block in enumerate(page.blocks):
                if block.kind == "figure" or not block.text.strip():
                    intentional[block.id] = "image_or_empty_region"
                    if block.kind == "figure":
                        flag("image_region", "Inspect image content; it is not legal text in this draft.", block=block)
                    continue
                if _is_header(block, document.title, repeated, page_note_labels):
                    intentional[block.id] = "repeated_page_header"
                    flag("candidate_page_header", "A likely running header was omitted from provisions; verify visually.", block=block)
                    continue
                if "\ufffd" in block.text:
                    flag("broken_text_encoding", "Stored text contains replacement characters; verify against PDF.", block=block)
                if block.kind == "table":
                    flag("table_requires_review", "Tabular reading order requires human review.", block=block)
                if _is_footnote(block):
                    in_footnotes = True
                if in_footnotes:
                    note_starts = list(_SOURCE_NOTE_LINE.finditer(block.text))
                    if note_starts and block.text[:note_starts[0].start()].strip():
                        if current_note is None:
                            current_note = make_node("other", None, "Unnumbered source note", None, block)
                        append(current_note, block, 0, note_starts[0].start(), scan_markers=False)
                    if not note_starts:
                        if current_note is None:
                            current_note = make_node("other", None, "Unnumbered source note", None, block)
                        append(current_note, block, 0, len(block.text), scan_markers=False)
                    for note_index, match in enumerate(note_starts):
                        label = match.group("label")
                        current_note = make_node("other", label, f"Source note {label}", None, block)
                        page_notes[label] = current_note
                        note_end = (note_starts[note_index + 1].start()
                                    if note_index + 1 < len(note_starts) else len(block.text))
                        append(current_note, block, match.start(), note_end, scan_markers=False)
                    flag("source_note_requires_review", "Footnote or amendment note retained separately; verify its meaning.", block=block)
                    continue
                body_start = _mixed_running_header_end(block, document.title)
                if body_start:
                    header = make_node("other", None, "Running header", None, block)
                    append(header, block, 0, body_start, scan_markers=False)
                    flag("candidate_page_header", "A running header was separated from body text in the same block.", block=block)
                body_text = block.text[body_start:]
                first_line = next((line.strip() for line in body_text.splitlines() if line.strip()), "")
                if (not in_contents and schedule is None and section is None
                        and _CONTENTS_HEADING.fullmatch(first_line)):
                    in_contents = True
                    current = make_node("other", None, "Contents", None, block)
                    append(current, block, body_start, len(block.text))
                    continue
                if in_contents:
                    lookahead = "\n".join(
                        item.text for item in page.blocks[block_index:block_index + 8]
                        if not _is_footnote(item)
                    )
                    transition = _contents_body_offset(body_text, document.title)
                    if transition is None and _contents_body_offset(lookahead, document.title) == 0:
                        transition = 0
                    if transition is None:
                        current = make_node("other", None, "Contents continuation", None, block)
                        append(current, block, body_start, len(block.text))
                        continue
                    if transition:
                        current = make_node("other", None, "Contents continuation", None, block)
                        append(current, block, body_start, body_start + transition)
                    in_contents = False
                    current = part = chapter = section = subsection = paragraph = list_parent = schedule = None
                    last_list_label = None
                    body_start += transition
                    body_text = block.text[body_start:]
                prior_list_items = (sum(1 for item in result.nodes
                                        if current is not None and current.kind == "paragraph"
                                        and item.kind == "paragraph" and item.parent_id == current.parent_id)
                                    if current is not None else 0)
                previous_line = next((line for line in reversed(current.own_text.splitlines())
                                      if line.strip()), "") if current is not None else ""
                next_block = (page.blocks[block_index + 1]
                              if block_index + 1 < len(page.blocks) else None)
                continuation_lookahead = (
                    next_block.text if next_block is not None
                    and next_block.kind == "paragraph" and not _is_footnote(next_block) else ""
                )
                boundaries = [(body_start + position, value)
                              for position, value in _line_boundaries(
                                  body_text, document.instrument_type, prior_list_items, previous_line,
                                  continuation_lookahead)]
                if section is not None and subsection is None and current is section:
                    limit = min(500, boundaries[0][0] - body_start if boundaries else len(body_text))
                    inline = _INLINE_CHILD.search(body_text, 0, limit)
                    if inline:
                        child = _boundary(inline.group("marker") + " ", document.instrument_type)
                        if child is not None and child[0] in {"subsection", "paragraph"}:
                            child_start = inline.start("amendment") if inline.group("amendment") else inline.start("marker")
                            boundaries.insert(0, (body_start + child_start, child))
                intervals = [(body_start, boundaries[0][0], None)] if boundaries and boundaries[0][0] > body_start else []
                intervals += [(position, boundaries[index + 1][0] if index + 1 < len(boundaries) else len(block.text), value)
                              for index, (position, value) in enumerate(boundaries)]
                if not boundaries:
                    intervals = [(body_start, len(block.text), None)]
                for start, end, heading_info in intervals:
                    if not block.text[start:end].strip():
                        continue
                    if (schedule is not None and (block.kind == "table" or heading_info is None
                            or heading_info[0] not in {"schedule", "paragraph", "ambiguous_one",
                                                    "post_list_continuation", "post_list_modal",
                                                    "proviso_continuation", "explanation"})):
                        append(schedule, block, start, end)
                        continue
                    if heading_info is None:
                        if current is None:
                            current = make_node("other", None, "Unclassified source text", None, block)
                            flag("unclassified_text", "Text before a recognized provision was retained as other.", block=block)
                        preceding = next((line for line in reversed(current.own_text.splitlines())
                                          if line.strip()), "")
                        caption_continues = (current is section and subsection is None
                                             and _unfinished_section_heading_line(preceding))
                        append(current, block, start, end)
                        if caption_continues:
                            caption = re.split(r"\.[ \t]*[-\u2013\u2014\ufffd]", block.text[start:end],
                                               maxsplit=1)[0].strip()
                            if caption and len(caption) < 240:
                                current.heading = " ".join(filter(None, (current.heading, caption)))[:240]
                        continue
                    kind, label, heading = heading_info
                    if (current is not None and kind in {"subsection", "paragraph", "section", "chapter", "part"}
                            and _is_wrapped_reference(block, start, current)):
                        append(current, block, start, end)
                        flag("wrapped_reference_not_child", "A line-wrapped legal cross-reference was retained as prose.", block=block)
                        continue
                    if (current is not None and kind in {"schedule", "rule", "article"}
                            and _is_wrapped_named_reference(block, start, current)):
                        append(current, block, start, end)
                        flag("wrapped_reference_not_child", "A line-wrapped reference to a Schedule, Rule or Article was retained as prose.", block=block)
                        continue
                    if (kind in {"chapter", "part"} and heading
                            and re.match(r"^of\b", heading, re.IGNORECASE)
                            and current is not None):
                        append(current, block, start, end)
                        flag("division_reference_not_heading",
                             "A division reference to another law was retained as prose.", block=block)
                        continue
                    if (kind == "section" and block.text[start:start + 8].casefold().startswith("section ")
                            and not heading and current is not None):
                        append(current, block, start, end)
                        flag("section_reference_not_heading",
                             "A bare sentence-level section reference was retained as prose.", block=block)
                        continue
                    if kind == "run_in_subsection":
                        consecutive = (subsection is not None and (subsection.label or "").isdigit()
                                       and int(label) == int(subsection.label) + 1)
                        first_child = subsection is None and label == "1"
                        if section is None or not (consecutive or first_child):
                            if current is None:
                                current = make_node("other", None, "Ambiguous run-in label", None, block)
                            append(current, block, start, end)
                            flag("run_in_subsection_needs_review",
                                 "A caption and numbered marker lack a reliable section sequence.", block=block)
                            continue
                        kind = "subsection"
                    if (kind in {"section", "rule", "article"} and not heading
                            and _isolated_heading_token(block.text[start:end], label)):
                        prior = current.own_text.rstrip() if current is not None else ""
                        note_anchor = (current is not None and label in page_note_labels
                                       and re.search(r"\b(?:18|19|20)\d{2}$", prior) is not None)
                        if note_anchor:
                            append(current, block, start, end)
                            marker = re.search(re.escape(label), block.text[start:end])
                            if marker:
                                page_markers.append((label, block, start + marker.start(),
                                                     start + marker.end(), current.id))
                            flag("isolated_source_marker",
                                 "A standalone number followed a dated source reference and matches a page note; it was not made a section.",
                                 block=block)
                        else:
                            current = make_node("other", None, "Ambiguous isolated number", None, block)
                            append(current, block, start, end)
                            flag("isolated_number_needs_review",
                                 "A marker-only heading lacks enough evidence to change the legal parent.", block=block)
                        continue
                    if kind == "ambiguous_one":
                        if section is not None and subsection is None and paragraph is None:
                            kind, label = "subsection", "1"
                            flag("ambiguous_numbering_glyph",
                                 "Native text reads (l), but adjacent (2) indicates (1); verify the PDF glyph. Raw text is unchanged.",
                                 block=block)
                        else:
                            kind = "paragraph"
                    if kind == "part":
                        part = make_node(kind, label, heading, None, block)
                        chapter = section = subsection = None
                        paragraph = list_parent = None
                        last_list_label = None
                        proviso_parent = None
                        current = part
                    elif kind == "chapter":
                        chapter = make_node(kind, label, heading, part, block)
                        section = subsection = None
                        paragraph = list_parent = None
                        last_list_label = None
                        proviso_parent = None
                        current = chapter
                    elif kind in {"section", "rule", "article"}:
                        if kind == "section":
                            heading = _completed_wrapped_section_heading(block.text[start:end]) or heading
                        section = make_node(kind, label, heading, chapter or part, block)
                        subsection = None
                        paragraph = list_parent = None
                        last_list_label = None
                        proviso_parent = None
                        current = section
                    elif kind == "subsection":
                        if section is None:
                            flag("orphan_subsection", "Numbered subsection has no recognized parent.", block=block)
                        subsection = make_node(kind, label, heading, section, block)
                        paragraph = list_parent = None
                        last_list_label = None
                        proviso_parent = None
                        current = subsection
                    elif kind == "paragraph":
                        if subsection is None and section is None and schedule is None:
                            flag("orphan_paragraph", "Lettered paragraph has no recognized parent.", block=block)
                        restoring_sibling = (
                            current is not None and current.kind == "other"
                            and current.heading == "Proviso" and proviso_parent is not None
                            and _continues_list(proviso_preceding_label, label)
                        )
                        sequential_sibling = (
                            current is not None and current.kind == "paragraph"
                            and _continues_list(current.label, label)
                            and label.lower() != "i"
                        )
                        starts_nested_list = (
                            list_parent is None and current is not None
                            and current.kind in {"paragraph", "other"}
                            and not restoring_sibling and not sequential_sibling
                            and (current.own_text.rstrip().endswith((":", "–", "—", "-", "\ufffd"))
                                 or current.heading in {"Explanation", "Proviso"})
                        )
                        continues_nested_list = (list_parent is not None
                                                 and _continues_list(last_list_label, label))
                        if starts_nested_list:
                            list_parent = current
                            last_list_label = label
                            current = make_node(kind, label, heading, list_parent, block)
                        elif continues_nested_list:
                            last_list_label = label
                            current = make_node(kind, label, heading, list_parent, block)
                        else:
                            list_parent = None
                            last_list_label = None
                            paragraph = make_node(kind, label, heading,
                                                  proviso_parent if restoring_sibling else subsection or section or schedule, block)
                            current = paragraph
                            proviso_parent = None
                    elif kind in {"post_list_continuation", "post_list_modal"}:
                        phrase = block.text[start:end].lstrip().casefold()
                        is_exclusion = phrase.startswith("but does not include")
                        is_resumed_subject = bool(re.match(
                            r"the\s+(?:[a-z][a-z-]*\s+){1,5}may\b", phrase, re.I,
                        ))
                        parent = (next((item for item in result.nodes if current is not None
                                        and item.id == current.parent_id), None)
                                  if current is not None else None)
                        siblings = (sum(item.kind == "paragraph" and item.parent_id == parent.id
                                        for item in result.nodes) if parent is not None else 0)
                        sibling_penalties = (any(item.id != current.id and item.kind == "paragraph"
                                                 and item.parent_id == parent.id
                                                 and _POST_LIST_MODAL.search(item.own_text)
                                                 for item in result.nodes)
                                             if current is not None and parent is not None else False)
                        next_parent = None
                        if list_parent is not None and is_exclusion:
                            next_parent = list_parent
                        elif (current is not None and current.kind == "paragraph" and parent is not None
                              and is_exclusion and not current.own_text.rstrip().endswith(",")):
                            next_parent = parent
                        elif (current is not None and current.kind == "paragraph" and parent is not None
                              and siblings >= 2 and is_resumed_subject
                              and current.own_text.rstrip().endswith((";", ":"))):
                            next_parent = parent
                        elif (current is not None and current.kind == "paragraph" and parent is not None
                              and siblings >= 2 and not sibling_penalties
                              and _open_parent_sentence(parent.own_text)):
                            next_parent = parent
                        if next_parent is not None:
                            current = next_parent
                            list_parent = None
                            last_list_label = None
                    elif kind == "proviso_continuation":
                        proviso_parent = subsection or section
                        proviso_preceding_label = current.label if current and current.kind == "paragraph" else None
                        current = make_node("other", None, "Proviso", proviso_parent, block)
                        paragraph = list_parent = None
                        last_list_label = None
                    elif kind == "explanation":
                        current = make_node("other", None, "Explanation", paragraph or subsection or section, block)
                        paragraph = list_parent = None
                        last_list_label = None
                    else:
                        current = make_node(kind, label, heading, None, block)
                        if kind in {"schedule", "preamble"}:
                            if kind == "schedule":
                                schedule = current
                            section = subsection = None
                            paragraph = list_parent = None
                            last_list_label = None
                    append(current, block, start, end)

            linked_note_labels: set[str] = set()
            marker_sites: dict[tuple[UUID, int, int], tuple[str, SourceBlock, int, int, UUID | None]] = {}
            for marker in page_markers:
                marker_sites.setdefault((marker[1].id, marker[2], marker[3]), marker)
            targets_by_label: dict[str, set[UUID | None]] = defaultdict(set)
            for label, _, _, _, target_id in marker_sites.values():
                targets_by_label[label].add(target_id)
            for label, marker_block, marker_start, marker_end, target_id in marker_sites.values():
                note = page_notes.get(label)
                if note is None:
                    continue
                if len(targets_by_label[label]) > 1:
                    flag("source_note_marker_ambiguous",
                         f"Source note {label} has markers on different provisions; review the intended links.",
                         block=marker_block)
                    continue
                result.note_links.append(DraftNoteLink(
                    note.id, target_id, label, marker_block.extraction_id,
                    marker_block.page_id, marker_block.id, marker_start, marker_end,
                ))
                linked_note_labels.add(label)
            for label in page_notes.keys() - linked_note_labels:
                flag("source_note_marker_unresolved",
                     f"Source note {label} has no matching printed marker on this page.", page=page)

    by_id = {node.id: node for node in result.nodes}
    for node in result.nodes:
        node.source_status = direct_source_status(node.kind, node.own_text)

    block_by_id = {block.id: block for document in documents
                   for page in document.pages for block in page.blocks}
    for link in result.note_links:
        note = by_id.get(link.note_provision_id)
        target = by_id.get(link.target_provision_id)
        if (not note or not target or target.kind not in LEGAL_KINDS
                or not is_star_only(target.own_text, target.label)
                or any(child.parent_id == target.id for child in result.nodes)):
            continue
        status, reason = linked_note_status(
            note.own_text, target_kind=target.kind, target_label=target.label,
        )
        if status is None:
            if re.search(r"\b(?:repealed|omitted|deleted)\b", note.own_text, re.I):
                flag("source_status_note_needs_review",
                     f"Linked note for {target.reference_path} does not clearly establish its status ({reason}).",
                     block=block_by_id.get(link.block_id))
            continue
        if target.source_status and target.source_status != status:
            flag("source_status_conflict",
                 f"Direct text and linked note disagree for {target.reference_path}; review both sources.",
                 block=block_by_id.get(link.block_id))
            continue
        target.source_status = status

    blocks = [block for document in documents for page in document.pages for block in page.blocks]
    for block in blocks:
        if block.id in intentional:
            result.decisions.append(BlockDecision(block.id, block.extraction_id, block.page_id,
                                                  "ignored", intentional[block.id]))
            continue
        ranges = sorted(mapped.get(block.id, []))
        if not ranges:
            status, reason = "unmapped", "no_provision_span"
            flag("unmapped_block", "Substantive block has no provision span.", block=block)
        else:
            cursor = 0
            gaps = []
            for start, end in ranges:
                if start < cursor:
                    raise ValueError("Overlapping provision spans would duplicate source text.")
                if block.text[cursor:start].strip():
                    gaps.append((cursor, start))
                cursor = end
            if block.text[cursor:].strip():
                gaps.append((cursor, len(block.text)))
            status, reason = ("partial", f"unmapped_ranges:{gaps}") if gaps else ("mapped", "exact_block_spans")
            if gaps:
                flag("partially_unmapped_block", f"Substantive source ranges were not assigned: {gaps}.", block=block)
        result.decisions.append(BlockDecision(block.id, block.extraction_id, block.page_id, status, reason))

    for node in result.nodes:
        for match in _REFERENCE.finditer(node.own_text):
            result.references.append(DraftReference(node.id, match.group()))
    result.coverage = {
        "total_pages": sum(document.page_count for document in documents),
        "recorded_pages": sum(len(document.pages) for document in documents),
        "total_blocks": len(blocks),
        "mapped_blocks": sum(item.status == "mapped" for item in result.decisions),
        "ignored_blocks": sum(item.status == "ignored" for item in result.decisions),
        "partial_blocks": sum(item.status == "partial" for item in result.decisions),
        "unmapped_blocks": sum(item.status == "unmapped" for item in result.decisions),
        "provisions": len(result.nodes),
        "exact_spans": len(result.spans),
        "unresolved_references": len(result.references),
    }
    return result
