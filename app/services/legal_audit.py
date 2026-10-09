"""Read-only checks over persisted draft provisions and source spans."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from app.services.legal_status import (
    LEGAL_KINDS, direct_source_status, is_star_only, linked_note_status,
)
from app.services.legal_tables import reviewed_region_is_lossless


_NUMBERED_HEADING = re.compile(
    r"(?m)^[ \t]*(?:\d{1,3}\[)?(?:(?:Section|Sec\.|Rule)[ \t]+)?"
    r"(?P<label>\d{1,3}(?:-[A-Za-z]+|[A-Za-z]*))\.[ \t]+(?=\S)",
    re.IGNORECASE,
)
_CONTENTS_MARKER = re.compile(
    r"\bC\s*O\s*N\s*T\s*E\s*N\s*T\s*S\b|\bTABLE OF CONTENTS\b",
    re.IGNORECASE,
)
_RUN_IN_SUBSECTION = re.compile(
    r"(?m)^[ \t]*[A-Z][A-Za-z][^.!?\n]{0,90}?\.[ \t]*"
    r"[-\u2013\u2014\ufffd][ \t]*\((?P<label>\d{1,3})\)(?=[ \t\n]|[A-Za-z])"
)
_POST_LIST_PENALTY = re.compile(
    r"[,;:][ \t]*(?:\n[ \t]*)?shall\b[^.;:]{0,120}"
    r"\b(?:punish(?:ed|able)|liable|sentenced|fined|imprisoned)\b",
    re.IGNORECASE,
)
_SCHEDULE_ITEM = re.compile(r"(?m)^[ \t]*\(([a-z])\)(?=[ \t]|[A-Za-z])")
_PENALTY = re.compile(
    r"\bshall\b[^.;:]{0,160}\b(?:punish(?:ed|able)|liable|sentenced|fined|imprisoned)\b",
    re.IGNORECASE,
)
_RUNNING_HEADER = re.compile(
    r"(?m)^\s*(?:The\s+)?[A-Z][^\n]{8,140}\b(?:Act|Ordinance|Rules?|Code)\b"
    r"[^\n]{0,60}\n\s*\d{1,4}\s*$", re.IGNORECASE,
)
_HEADING_CAPTION_START = re.compile(
    r"^[ \t]*[A-Z][A-Za-z][^.!?\n]{0,100}\.[ \t]*[-\u2013\u2014\ufffd][ \t]*\(1\)",
)
_MARKER_ONLY_HEADING = re.compile(
    r"^\s*(?:\d{1,3}\[)?(?:(?:Section|Sec\.)\s+)?\d+[A-Za-z-]*\.\s*$",
    re.IGNORECASE,
)


def _open_list_subject(text: str) -> bool:
    matches = list(re.finditer(
        r"\b(?:whoever|whosoever|where|when|if|(?:any|every|a)\s+person(?:\s+who)?)\b",
        text, re.IGNORECASE,
    ))
    return bool(matches and not re.search(r"\b(?:shall|must|may)\b",
                                          text[matches[-1].start():], re.IGNORECASE))


def _numbered_labels(text: str) -> set[str]:
    return {match.group("label").casefold() for match in _NUMBERED_HEADING.finditer(text)}


def source_page_review_findings(source_pages: Iterable, latest_review: dict) -> list[dict]:
    """Block source-level ambiguities that exact structure spans cannot detect."""
    findings = []
    for page_id, filename, page_index, outcome, warnings, table_regions in source_pages:
        unreviewed_tables = [region for region in table_regions or []
                             if not reviewed_region_is_lossless(region)]
        if unreviewed_tables:
            findings.append({
                "code": "table_cells_need_review", "severity": "error",
                "message": (f"{filename} page {page_index} has {len(unreviewed_tables)} unreviewed table "
                            "candidate(s). Compare each row and cell with the PDF; "
                            "flat provision text is not a verified table transcription."),
                "provision_id": None, "reference_path": None,
            })
        review_codes = [item.get("code", "unknown") for item in warnings or []
                        if item.get("code") in {"blank_candidate", "ambiguous_numbering_glyph"}]
        if latest_review.get(page_id) is not None and latest_review[page_id].decision == "mark_blank":
            review_codes = [code for code in review_codes if code != "blank_candidate"]
        if outcome == "needs_review" and review_codes:
            findings.append({
                "code": "source_page_needs_review", "severity": "error",
                "message": f"{filename} page {page_index} needs source inspection: {', '.join(review_codes)}",
                "provision_id": None, "reference_path": None,
            })
    return findings


def structure_findings(
    provisions: Iterable, span_rows: Iterable[tuple], *,
    source_blocks: Iterable = (), instrument_type: str | None = None,
    note_links: Iterable = (),
) -> list[dict]:
    """Flag suspicious saved data; findings are prompts for review, not legal conclusions."""
    nodes = list(provisions)
    span_rows = list(span_rows)
    source_blocks = list(source_blocks)
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
        body_signal = any(re.search(r"\b(?:Preamble|WHEREAS)\b", block.normalized_text or "", re.I)
                          for block in source_blocks if block.block_kind not in {"footnote", "note"})
        if (len(apparent) >= 3 or len(apparent) >= 2 and body_signal) and not saved:
            findings.append({
                "code": "no_operative_structure", "severity": "error",
                "message": (f"The source contains {len(apparent)} distinct apparent numbered "
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
        swallowed_body = any(
            re.search(r"\b(?:Preamble|WHEREAS)\b", node.own_text or "", re.I)
            and len(_numbered_labels(node.own_text or "")) >= 2
            for node in nodes if node.kind == "other"
            and (getattr(node, "heading", None) or "").casefold().startswith("contents")
        )
        if swallowed_body or len(contents_labels) >= 3 and len(saved) * 5 <= len(contents_labels):
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

    children: dict[object, list] = defaultdict(list)
    for node in nodes:
        children[getattr(node, "parent_id", None)].append(node)

    def add(code: str, message: str, node, severity: str = "error") -> None:
        findings.append({"code": code, "severity": severity, "message": message,
                         "provision_id": node.id, "reference_path": node.reference_path})

    for node in nodes:
        parent = nodes_by_id.get(getattr(node, "parent_id", None))
        own_text = node.own_text or ""
        explicit_status = direct_source_status(node.kind, own_text)
        if explicit_status and getattr(node, "source_status", None) != explicit_status:
            add("explicit_source_status_missing",
                f"Printed provision says {explicit_status}, but its saved source_status does not.",
                node)
        if node.kind == "subsection" and parent is not None and parent.kind not in {"section", "rule", "article"}:
            add("invalid_subsection_parent", "A subsection is not below a section, rule, or article.", node)
        if node.kind == "section" and parent is not None and parent.kind not in {"part", "chapter"}:
            add("invalid_section_parent", "A section has an implausible legal parent.", node)
        if node.kind in {"section", "rule", "article"}:
            child_labels = {child.label for child in children[node.id] if child.kind == "subsection"}
            if ("2" in child_labels or "3" in child_labels) and "1" not in child_labels and re.search(r"\(1\)", own_text):
                add("missing_first_subsection", "A printed (1) remains in parent text while later subsections have nodes.", node)
            if re.fullmatch(r"\s*(?:section|sec\.)\s+\d+[A-Za-z]*\.\s*", own_text, re.I):
                add("inline_reference_as_section", "A bare prose section reference was saved as an operative section.", node)
            if re.fullmatch(r"\s*(?:(?:section|sec\.|rule|article)\s+)?\d+[A-Za-z]*\.\s*", own_text, re.I):
                add("isolated_marker_as_section", "A marker-only token was saved as an operative heading.",
                    node, severity="warning")
                if children[node.id]:
                    add("weak_heading_reparenting", "A marker-only heading has legal children; inspect their true parent.",
                        node)
            first_children = [child for child in children[node.id]
                              if child.kind == "subsection" and str(child.label) == "1"]
            if (first_children and _HEADING_CAPTION_START.match(first_children[0].own_text or "")
                    and (_MARKER_ONLY_HEADING.fullmatch(own_text)
                         or not re.search(r"[.!?\u2013\u2014\ufffd]\s*$", own_text.rstrip()))):
                add("section_caption_in_first_subsection",
                    "A run-in caption appears in subsection (1) after an unfinished section heading.", node)
        if node.kind in {"section", "subsection"}:
            visible_run_ins = {match.group("label") for match in _RUN_IN_SUBSECTION.finditer(own_text)}
            if node.kind == "subsection" and node.label:
                visible_run_ins.discard(str(node.label))
                siblings = children[getattr(node, "parent_id", None)]
            else:
                siblings = children[node.id]
            saved_siblings = {str(sibling.label) for sibling in siblings
                              if sibling.kind == "subsection" and sibling.label is not None}
            if visible_run_ins - saved_siblings:
                add("embedded_subsection_missing",
                    "A run-in subsection marker remains inside another provision instead of a sibling node.", node)
        if node.kind == "schedule":
            visible_items = {match.group(1) for match in _SCHEDULE_ITEM.finditer(own_text)}
            saved_items = {str(child.label).lower() for child in children[node.id]
                           if child.kind == "paragraph" and child.label is not None}
            if len(visible_items) >= 2 and len(visible_items - saved_items) >= 2:
                node_rows = spans_by_node.get(node.id, [])
                table_only = bool(node_rows and all(getattr(block, "block_kind", None) == "table"
                                                    for _, block in node_rows if block is not None))
                if not table_only:
                    add("schedule_items_unstructured",
                        "Multiple visible schedule list labels remain in the schedule text without child nodes.",
                        node, severity="warning")
        if node.kind in {"part", "chapter"} and re.search(
            r"\b(?:PART|CHAPTER)\s+[IVXLCDM\d]+\s+of\b", own_text[:160], re.I,
        ):
            add("sentence_reference_as_division", "A sentence-level division reference was saved as a heading.", node)
            if any(child.kind == "section" for child in children[node.id]):
                add("late_division_reparenting", "Later sections were parented under an apparent division reference.", node)
        if node.kind == "paragraph":
            if parent is not None and parent.kind == "paragraph":
                prior = (parent.label or "").lower()
                current = (node.label or "").lower()
                if (len(prior) == len(current) == 1 and prior.isalpha() and current.isalpha()
                        and ord(current) == ord(prior) + 1):
                    add("sequential_sibling_nested", "A sequential paragraph appears nested below its predecessor.", node)
            if parent is not None and parent.kind == "other" and getattr(parent, "heading", None) == "Proviso":
                grandparent = nodes_by_id.get(getattr(parent, "parent_id", None))
                siblings = children.get(grandparent.id, []) if grandparent is not None else []
                prior_labels = {sibling.label for sibling in siblings if sibling.kind == "paragraph"}
                if any(label and len(label) == len(node.label or "") == 1
                       and ord((node.label or "").lower()) == ord(label.lower()) + 1
                       for label in prior_labels):
                    add("sibling_beneath_proviso", "A later sibling paragraph was nested under a proviso.", node)
            if (re.search(r"[,;:]\s*\n\s*(?:shall|but\s+does|the\s+\w+(?:\s+\w+){0,3}\s+may)\b", own_text, re.I)
                    and parent is not None and (parent.kind == "paragraph" or
                                                 any(sibling.kind == "paragraph" and sibling.id != node.id
                                                     for sibling in children[parent.id]))):
                add("post_list_continuation_in_child", "A post-list continuation may be attached to the last child.", node,
                    severity="warning")
            elif (parent is not None and _POST_LIST_PENALTY.search(own_text)
                  and _open_list_subject(parent.own_text or "")
                  and len([sibling for sibling in children[parent.id]
                           if sibling.kind == "paragraph"]) >= 2):
                add("post_list_continuation_in_child",
                    "A shared penalty appears attached to the final child of an open parent list.",
                    node, severity="warning")
            if parent is not None and parent.kind in {"section", "subsection"}:
                siblings = [child for child in children[parent.id] if child.kind == "paragraph"]
                earlier = [item for item in siblings if item.id != node.id
                           and getattr(item, "ordinal", 0) < getattr(node, "ordinal", 0)]
                if (earlier and any(_PENALTY.search(item.own_text or "") for item in earlier)
                        and _PENALTY.search(parent.own_text or "")
                        and not _PENALTY.search(own_text)
                        and re.search(r"(?:,|\bshall)\s*$", own_text.rstrip(), re.I)):
                    add("item_penalty_moved_to_parent",
                        "A list item ends at 'shall' while earlier siblings retain their own penalties; "
                        "its penalty may have been moved to the parent.", node,
                        severity="warning")
    for span, block in span_rows:
        node = nodes_by_id.get(span.provision_id)
        if node is None or node.kind not in LEGAL_KINDS or block is None:
            continue
        bbox = getattr(block, "bbox", None) or {}
        text = getattr(block, "normalized_text", "") or ""
        start, end = getattr(span, "block_char_start", None), getattr(span, "block_char_end", None)
        if (start is not None and end is not None and isinstance(bbox, dict)
                and float(bbox.get("y0", 1e9)) < 120
                and _RUNNING_HEADER.search(text[start:end])):
            add("running_header_in_operative_text",
                "A page-top law title and page number are assigned to operative provision text.", node)
    for link in note_links:
        note = nodes_by_id.get(link.note_provision_id)
        target = nodes_by_id.get(link.target_provision_id)
        if (note is None or target is None or target.kind not in LEGAL_KINDS
                or not is_star_only(target.own_text or "", target.label)):
            continue
        expected, reason = linked_note_status(
            note.own_text or "", target_kind=target.kind, target_label=target.label,
        )
        if expected and getattr(target, "source_status", None) != expected:
            add("linked_note_source_status_missing",
                f"Exactly linked source note says {expected}, but the saved target status does not.",
                target)
        elif reason and re.search(r"\b(?:repealed|omitted|deleted)\b", note.own_text or "", re.I):
            add("linked_note_status_needs_review",
                f"Star-only target has a status-related note without an unambiguous match ({reason}).",
                target, severity="warning")
    return findings
