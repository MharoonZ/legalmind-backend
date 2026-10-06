"""Conservative, deterministic draft parsing of stored legal-PDF blocks.

The parser never rewrites source text. Each non-whitespace character assigned to
a provision remains an exact slice of a stored FileBlock. Ambiguity is reported
for human review; this output is not an approved legal transcription.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4


PARSER_VERSION = "legal-structure-v3"
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
    decisions: list[BlockDecision] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        payload = {
            "parser": PARSER_VERSION,
            "nodes": [(n.reference_path, n.kind, n.label, n.heading, n.own_text)
                      for n in self.nodes],
            "spans": [(s.provision_char_start, s.provision_char_end,
                       str(s.block_id), s.block_char_start, s.block_char_end)
                      for s in self.spans],
            "decisions": [(str(d.block_id), d.status, d.reason) for d in self.decisions],
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()


_REFERENCE = re.compile(
    r"\b(?:sections?|rules?|articles?)\s+\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*"
    r"|\b(?:First|Second|Third|Fourth|Fifth|Sixth|[IVXLCDM]+)\s+Schedule\b"
    r"|\bSchedule\s+(?:[IVXLCDM]+|\d+[A-Za-z]*)\b", re.IGNORECASE,
)
_FOOTNOTE_START = re.compile(
    r"^\s*\d{1,3}(?:Substituted|Ibid\b|Short\b|Omitted|Repealed|Inserted|XLV\b|The\b)",
    re.IGNORECASE,
)
_CONTENTS = re.compile(r"\bC\s*O\s*N\s*T\s*E\s*N\s*T\s*S\b|\bTABLE OF CONTENTS\b", re.IGNORECASE)
_SECTION = re.compile(r"^(?:(?:Section|Sec\.)[ \t]+)?(\d+[A-Z]*(?:\([A-Za-z0-9]+\))*)\.[ \t]+")
_EXPLICIT_SECTION = re.compile(r"^(?:Section|Sec\.)[ \t]+(\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*)[.:\-–—][ \t]*", re.IGNORECASE)
_EXPLICIT_RULE = re.compile(r"^Rule[ \t]+(\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*)[. :\-–—][ \t]*", re.IGNORECASE)
_EXPLICIT_ARTICLE = re.compile(r"^Article[ \t]+(\d+[A-Za-z]*(?:\([A-Za-z0-9]+\))*)[. :\-–—][ \t]*", re.IGNORECASE)
_PART = re.compile(r"^PART[ \t]+([IVXLCDM]+|\d+[A-Za-z]*)\b", re.IGNORECASE)
_CHAPTER = re.compile(r"^CHAPTER[ \t]+([IVXLCDM]+|\d+[A-Za-z]*)\b", re.IGNORECASE)
_SCHEDULE = re.compile(r"^(?:(FIRST|SECOND|THIRD|FOURTH|FIFTH|[IVXLCDM]+)[ \t]+SCHEDULE|SCHEDULE[ \t]+([IVXLCDM]+|\d+[A-Za-z]*))\b", re.IGNORECASE)
_PAREN = re.compile(r"^\(([A-Za-z0-9]+)\)[ \t]+")
_PREAMBLE = re.compile(r"^Preamble\b[. :\-–—]*", re.IGNORECASE)


def _boundary(line: str, instrument_type: str) -> tuple[str, str | None, str | None] | None:
    candidate = line.lstrip(" \t")
    for kind, expression in (("part", _PART), ("chapter", _CHAPTER),
                             ("schedule", _SCHEDULE), ("preamble", _PREAMBLE),
                             ("rule", _EXPLICIT_RULE), ("article", _EXPLICIT_ARTICLE),
                             ("section", _EXPLICIT_SECTION)):
        match = expression.match(candidate)
        if match:
            label = next((group for group in match.groups() if group), None)
            return kind, label, "Preamble" if kind == "preamble" else _heading(candidate[match.end():])
    match = _SECTION.match(candidate)
    if match:
        return ("rule" if instrument_type == "rule" else "section",
                match.group(1), _heading(candidate[match.end():]))
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


def _line_boundaries(text: str, instrument_type: str):
    for match in re.finditer(r"[^\n]+", text):
        value = _boundary(match.group(), instrument_type)
        if value is not None:
            position = match.start() + len(match.group()) - len(match.group().lstrip(" \t"))
            yield position, value


def _signature(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "", text.casefold())).strip()


def _is_header(block: SourceBlock, title: str, repeated: set[str]) -> bool:
    if block.ordinal != 1 or not block.bbox or not block.page_height:
        return False
    if float(block.bbox.get("y0", 1e9)) > block.page_height * 0.16:
        return False
    text = block.text.strip()
    if len(text) > 160:
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


def _is_footnote(block: SourceBlock) -> bool:
    if block.kind in {"footnote", "note"}:
        return True
    return bool(block.page_height and block.bbox
                and float(block.bbox.get("y0", 0)) > block.page_height * 0.45
                and _FOOTNOTE_START.match(block.text))


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
    counters: dict[str, int] = {}
    current_prefix = ""

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

    def append(node: DraftNode, block: SourceBlock, start: int, end: int) -> None:
        text = block.text[start:end]
        if not text.strip():
            return
        separator = "\n" if node.own_text else ""
        output_start = len(node.own_text) + len(separator)
        node.own_text += separator + text
        result.spans.append(DraftSpan(node.id, block.extraction_id, block.page_id,
                                      block.id, output_start, output_start + len(text), start, end))
        mapped.setdefault(block.id, []).append((start, end))

    for document in documents:
        current_prefix = f"f:{document.file_id}"
        current = part = chapter = section = subsection = None
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
            in_footnotes = False
            if not page.blocks and in_contents:
                in_contents = False
                current = None
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
            for block in page.blocks:
                if block.kind == "figure" or not block.text.strip():
                    intentional[block.id] = "image_or_empty_region"
                    if block.kind == "figure":
                        flag("image_region", "Inspect image content; it is not legal text in this draft.", block=block)
                    continue
                if _is_header(block, document.title, repeated):
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
                    node = make_node("other", None, "Source note / amendment note", None, block)
                    append(node, block, 0, len(block.text))
                    flag("source_note_requires_review", "Footnote or amendment note retained separately; verify its meaning.", block=block)
                    continue
                if _CONTENTS.search(block.text):
                    in_contents = True
                    current = make_node("other", None, "Contents", None, block)
                    append(current, block, 0, len(block.text))
                    continue
                boundaries = list(_line_boundaries(block.text, document.instrument_type))
                if in_contents and not any(kind in {"preamble", "part", "chapter"}
                                           for _, (kind, _, _) in boundaries):
                    current = make_node("other", None, "Contents continuation", None, block)
                    append(current, block, 0, len(block.text))
                    continue
                if boundaries:
                    in_contents = False
                intervals = [(0, boundaries[0][0], None)] if boundaries and boundaries[0][0] else []
                intervals += [(position, boundaries[index + 1][0] if index + 1 < len(boundaries) else len(block.text), value)
                              for index, (position, value) in enumerate(boundaries)]
                if not boundaries:
                    intervals = [(0, len(block.text), None)]
                for start, end, heading_info in intervals:
                    if not block.text[start:end].strip():
                        continue
                    if heading_info is None:
                        if current is None:
                            current = make_node("other", None, "Unclassified source text", None, block)
                            flag("unclassified_text", "Text before a recognized provision was retained as other.", block=block)
                        append(current, block, start, end)
                        continue
                    kind, label, heading = heading_info
                    if kind == "part":
                        part = make_node(kind, label, heading, None, block)
                        chapter = section = subsection = None
                        current = part
                    elif kind == "chapter":
                        chapter = make_node(kind, label, heading, part, block)
                        section = subsection = None
                        current = chapter
                    elif kind in {"section", "rule", "article"}:
                        section = make_node(kind, label, heading, chapter or part, block)
                        subsection = None
                        current = section
                    elif kind == "subsection":
                        if section is None:
                            flag("orphan_subsection", "Numbered subsection has no recognized parent.", block=block)
                        subsection = make_node(kind, label, heading, section, block)
                        current = subsection
                    elif kind == "paragraph":
                        if subsection is None and section is None:
                            flag("orphan_paragraph", "Lettered paragraph has no recognized parent.", block=block)
                        current = make_node(kind, label, heading, subsection or section, block)
                    else:
                        current = make_node(kind, label, heading, None, block)
                        if kind in {"schedule", "preamble"}:
                            section = subsection = None
                    append(current, block, start, end)

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
