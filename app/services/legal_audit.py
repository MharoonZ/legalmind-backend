"""Read-only checks over persisted draft provisions and source spans."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable


_NUMBERED_HEADING = re.compile(
    r"(?m)^[ \t]*(?:\d{1,3}\[)?(?:(?:Section|Sec\.|Rule)[ \t]+)?"
    r"(?P<label>\d{1,3}(?:-[A-Za-z]+|[A-Za-z]*))\.[ \t]+(?=\S)",
    re.IGNORECASE,
)
_CONTENTS_MARKER = re.compile(
    r"\bC\s*O\s*N\s*T\s*E\s*N\s*T\s*S\b|\bTABLE OF CONTENTS\b",
    re.IGNORECASE,
)


def _numbered_labels(text: str) -> set[str]:
    return {match.group("label").casefold() for match in _NUMBERED_HEADING.finditer(text)}


def structure_findings(
    provisions: Iterable, span_rows: Iterable[tuple], *,
    source_blocks: Iterable = (), instrument_type: str | None = None,
) -> list[dict]:
    """Flag suspicious saved data; findings are prompts for review, not legal conclusions."""
    nodes = list(provisions)
    span_rows = list(span_rows)
    nodes_by_id = {node.id: node for node in nodes}
    schedule_ids = {node.id for node in nodes if node.kind == "schedule"}
    schedule_block_ids = {getattr(span, "block_id", None) for span, _ in span_rows
                          if span.provision_id in schedule_ids}

    def in_schedule(node) -> bool:
        parent_id = getattr(node, "parent_id", None)
        while parent_id is not None:
            if parent_id in schedule_ids:
                return True
            parent = nodes_by_id.get(parent_id)
            parent_id = getattr(parent, "parent_id", None) if parent is not None else None
        return False

    spans_by_node: dict[object, list[tuple]] = defaultdict(list)
    for span, block in span_rows:
        spans_by_node[span.provision_id].append((span, block))

    findings: list[dict] = []
    numbered: dict[str, set[int]] = defaultdict(set)
    labels: dict[tuple[str, str], list] = defaultdict(list)
    for node in nodes:
        if node.kind in {"section", "rule"} and node.label and not in_schedule(node):
            label = node.label.strip().casefold()
            labels[(node.kind, label)].append(node)
            if re.fullmatch(r"[1-9]\d*", label):
                numbered[node.kind].add(int(label))

        text = node.own_text or ""
        if not text.strip():
            continue
        rows = spans_by_node.get(node.id, [])
        if not rows:
            findings.append({"code": "missing_source_span", "severity": "error",
                             "message": "Saved provision text has no source span.",
                             "provision_id": node.id, "reference_path": node.reference_path})
            continue
        covered = [False] * len(text)
        mismatch = False
        for span, block in rows:
            start, end = span.provision_char_start, span.provision_char_end
            source_start, source_end = span.block_char_start, span.block_char_end
            source_text = block.normalized_text if block is not None else ""
            if (span.mapping_quality != "exact" or not 0 <= start < end <= len(text)
                    or source_start is None or source_end is None
                    or not 0 <= source_start < source_end <= len(source_text)
                    or text[start:end] != source_text[source_start:source_end]):
                mismatch = True
                continue
            covered[start:end] = [True] * (end - start)
        if mismatch:
            findings.append({"code": "span_text_mismatch", "severity": "error",
                             "message": "At least one saved span does not match its source block and provision offsets.",
                             "provision_id": node.id, "reference_path": node.reference_path})
        if any(not matched and not char.isspace() for char, matched in zip(text, covered)):
            findings.append({"code": "uncovered_provision_text", "severity": "error",
                             "message": "Some saved provision characters have no matching exact source span.",
                             "provision_id": node.id, "reference_path": node.reference_path})

    for (kind, label), duplicates in labels.items():
        if len(duplicates) > 1:
            findings.append({"code": "possible_duplicate_label", "severity": "warning",
                             "message": f"{kind.title()} {label} occurs {len(duplicates)} times in this revision; check the source and hierarchy.",
                             "provision_id": None, "reference_path": None})
    for kind, values in numbered.items():
        ordered = sorted(values)
        for left, right in zip(ordered, ordered[1:]):
            if right - left > 1:
                missing = f"{left + 1}" if right - left == 2 else f"{left + 1}–{right - 1}"
                findings.append({"code": "possible_number_gap", "severity": "warning",
                                 "message": f"No saved {kind} node for number {missing} between {left} and {right}; verify against the operative PDF.",
                                 "provision_id": None, "reference_path": None})

    # Full block coverage is not evidence that the operative law was parsed:
    # a stalled contents parser can map every character into "other" nodes.
    # Compare apparent numbered source headings with the saved legal tree.
    if instrument_type in {"act", "rule", "ordinance", "regulation"}:
        operative_kind = "rule" if instrument_type == "rule" else "section"
        apparent = set()
        source_pages = {}
        contents_pages = set()
        for block in source_blocks:
            source_text = block.normalized_text or ""
            eligible = block.block_kind not in {"footnote", "note", "header", "footer", "figure"}
            if eligible and getattr(block, "id", None) not in schedule_block_ids:
                apparent.update(_numbered_labels(source_text))
            extraction_id = getattr(block, "extraction_id", None)
            page_id = getattr(block, "page_id", None)
            page_index = getattr(block, "page_index", None)
            if extraction_id is not None and page_id is not None and page_index is not None:
                source_pages[(extraction_id, page_id)] = page_index
                if eligible and _CONTENTS_MARKER.search(source_text):
                    contents_pages.add((extraction_id, page_index))
        saved = {node.label.strip().casefold() for node in nodes
                 if node.kind == operative_kind and node.label and node.label.strip()
                 and not in_schedule(node)}
        if len(apparent) >= 3 and not saved:
            findings.append({
                "code": "no_operative_structure", "severity": "error",
                "message": (f"The source contains at least {len(apparent)} distinct apparent numbered "
                            f"headings, but this draft has no saved {operative_kind} nodes. "
                            "Compare the source PDF before approving or indexing this revision."),
                "provision_id": None, "reference_path": None,
            })
        elif len(apparent) >= 5 and len(saved) * 5 <= len(apparent) and len(apparent - saved) >= 3:
            findings.append({
                "code": "too_few_operative_sections", "severity": "error",
                "message": (f"The source contains at least {len(apparent)} distinct apparent numbered "
                            f"headings but only {len(saved)} saved {operative_kind} labels. "
                            "The legal tree may be incomplete; compare the source PDF before approval or indexing."),
                "provision_id": None, "reference_path": None,
            })
        contents_labels = set().union(*(
            _numbered_labels(node.own_text or "") for node in nodes
            if node.kind == "other" and (node.heading or "").casefold().startswith("contents")
        ))
        if len(contents_labels) >= 3 and len(saved) * 5 <= len(contents_labels):
            findings.append({
                "code": "contents_mode_into_body", "severity": "error",
                "message": (f"Saved Contents text contains {len(contents_labels)} distinct apparent "
                            f"numbered headings while only {len(saved)} {operative_kind} labels were saved. "
                            "The parser may have treated operative body pages as contents; inspect the PDF and page mappings."),
                "provision_id": None, "reference_path": None,
            })
        # A substantial repeated run from a page explicitly headed Contents
        # is stronger evidence than duplicate labels alone. Require distinct
        # saved nodes, the same source PDF, later pages, and longer body text.
        # A small or cross-document repeat remains only a review warning.
        node_pages: dict[object, set[tuple[object, int]]] = defaultdict(set)
        for span, _ in span_rows:
            page_index = source_pages.get((getattr(span, "extraction_id", None),
                                           getattr(span, "page_id", None)))
            if page_index is not None:
                node_pages[span.provision_id].add((span.extraction_id, page_index))
        repeated_from_contents = set()
        for (kind, label), duplicates in labels.items():
            if kind != operative_kind or len(duplicates) < 2:
                continue
            for front in duplicates:
                front_locations = node_pages.get(front.id, set()) & contents_pages
                if not front_locations:
                    continue
                for later in duplicates:
                    if later.id == front.id or len((later.own_text or "").strip()) < len((front.own_text or "").strip()) + 40:
                        continue
                    if any(other_extraction == extraction and other_page >= first_page + 2
                           for extraction, first_page in front_locations
                           for other_extraction, other_page in node_pages.get(later.id, set())):
                        repeated_from_contents.add(label)
                        break
                if label in repeated_from_contents:
                    break
        if len(repeated_from_contents) >= 5:
            findings.append({
                "code": "contents_entries_as_provisions", "severity": "error",
                "message": (f"At least {len(repeated_from_contents)} {operative_kind} labels have one saved "
                            "node on an explicit Contents page and another, longer node on later pages "
                            "of the same PDF. Contents entries appear to have been saved as operative "
                            "provisions; inspect the PDF before approving or indexing this revision."),
                "provision_id": None, "reference_path": None,
            })
    return findings
