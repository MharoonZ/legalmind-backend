"""Conservative, source-traceable candidate rows for legal-PDF tables."""

from __future__ import annotations

import re


_NUMBERED_ITEM = re.compile(r"(?m)^[ \t]*(?:\d+[.)]|\([ivxlcdm]+\))[ \t]*(?=\S)", re.I)
_UNNUMBERED_SECTION = re.compile(r"(?im)^[ \t]*Sections?\b")
_SENTENCE_END = re.compile(r"\.(?=\n[ \t]*[A-Z])")


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _cell_text(cell: dict) -> str:
    return cell.get("text") or ""


def _slices(value: str, starts: list[int]) -> list[dict]:
    if value[:starts[0]].strip():
        raise ValueError("Table cell has unmatched text before its first item.")
    result = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(value)
        piece = value[start:end]
        left = len(piece) - len(piece.lstrip())
        right = len(piece.rstrip())
        result.append({"start": start + left, "end": start + right,
                       "text": piece.strip()})
    if _compact(" ".join(item["text"] for item in result)) != _compact(value):
        raise ValueError("Split table cell does not reproduce its source text.")
    return result


def _split_for_count(value: str, count: int) -> list[dict] | None:
    """Only use visible boundaries that produce exactly `count` items."""
    for pattern in (_NUMBERED_ITEM, _UNNUMBERED_SECTION):
        starts = [match.start() for match in pattern.finditer(value)]
        if len(starts) == count:
            return _slices(value, starts)
    starts = [0] + [match.end() + 1 for match in _SENTENCE_END.finditer(value)]
    starts = sorted(set(start for start in starts if start < len(value)))
    return _slices(value, starts) if len(starts) == count else None


def atomize_table_region(
    region: dict, *, inherited_enactment: str | None = None,
) -> tuple[list[dict], str | None]:
    """Split aligned numbered row groups in any width table, or fail closed.

    The first column may be a repeated/blank group label. Other populated
    columns must have exact independent boundaries; alignment is never guessed.
    """
    columns = region["column_count"]
    if not isinstance(columns, int) or columns < 2:
        raise ValueError("A table needs at least two columns.")
    atomic: list[dict] = []
    current_group = inherited_enactment
    for row_index, row in enumerate(region["rows"]):
        cells = [_cell_text(cell) for cell in row]
        if len(cells) != columns:
            raise ValueError("Detected table row has the wrong number of cells.")
        counts = {len(_NUMBERED_ITEM.findall(value)) for value in cells
                  if len(_NUMBERED_ITEM.findall(value)) > 1}
        # Three-column enactment/section/penalty tables may have unnumbered
        # penalties on separate sentence lines. Keep a mismatch gate for them.
        penalty_count = (len(_SENTENCE_END.findall(cells[2])) + 1
                         if columns == 3 and row_index >= 2 and cells[1] and cells[2]
                         else 1)
        if penalty_count > 1:
            counts.add(penalty_count)
        if len(counts) > 1:
            raise ValueError(f"Table row {row_index} has incompatible item counts {sorted(counts)}.")
        count = next(iter(counts), 1)
        if count == 1:
            atomic.append({"source_row_index": row_index, "item_index": 1,
                           "row_type": "header" if row_index < 2 else "entry",
                           "cells": cells, "source_slices": [None] * columns})
            if row_index >= 2 and cells[0]:
                current_group = cells[0]
            continue
        pieces: list[list[dict] | None] = []
        for column, value in enumerate(cells):
            split = _split_for_count(value, count) if value else None
            if split is None and column != 0 and value:
                raise ValueError(
                    f"Table row {row_index} has {count} items but column {column + 1} "
                    "has no matching boundaries."
                )
            pieces.append(split)
        if pieces[0] is None:
            if cells[0]:
                current_group = cells[0]
            if not current_group:
                raise ValueError("A continued table row has no preceding group label.")
        for item_index in range(count):
            row_cells = []
            slices = []
            for column in range(columns):
                split = pieces[column]
                if split is not None:
                    piece = split[item_index]
                    row_cells.append(piece["text"])
                    slices.append({"start": piece["start"], "end": piece["end"]})
                else:
                    row_cells.append(current_group if column == 0 else "")
                    slices.append(None)
            atomic.append({"source_row_index": row_index, "item_index": item_index + 1,
                           "row_type": "entry", "cells": row_cells,
                           "source_slices": slices,
                           "enactment_inherited": pieces[0] is None and not bool(cells[0])})
        if cells[0]:
            current_group = cells[0]
    return atomic, current_group


def reviewed_region_is_lossless(region: dict) -> bool:
    """Reject edits that omit, duplicate, or alter original detected cells."""
    if (region.get("status") != "visually_reviewed_rows"
            or not region.get("reviewed_by") or not region.get("review_note")
            or not region.get("atomic_rows")):
        return False
    columns = region.get("column_count")
    rows = region.get("rows") or []
    atomic = region["atomic_rows"]
    if (not isinstance(columns, int) or columns < 2 or not rows
            or region.get("row_count", len(rows)) != len(rows)):
        return False
    if any(not isinstance(item.get("source_row_index"), int)
           or not 0 <= item["source_row_index"] < len(rows) for item in atomic):
        return False
    for row_index, source_row in enumerate(rows):
        if len(source_row) != columns:
            return False
        entries = [item for item in atomic if item["source_row_index"] == row_index]
        if not entries or [item.get("item_index", index + 1) for index, item in enumerate(entries)] != list(range(1, len(entries) + 1)):
            return False
        source = [_cell_text(cell) for cell in source_row]
        if len(entries) == 1:
            if entries[0].get("cells") != source:
                return False
            continue
        if any(item.get("row_type") != "entry" or len(item.get("cells", [])) != columns
               or len(item.get("source_slices", [])) != columns for item in entries):
            return False
        for column, source_text in enumerate(source):
            spans = [item["source_slices"][column] for item in entries]
            values = [item["cells"][column] for item in entries]
            if all(span is None for span in spans):
                if not source_text and not any(values):
                    continue
                if column != 0 or not all(value == values[0] and value for value in values):
                    return False
                if source_text and values[0] != source_text:
                    return False
                continue
            if any(span is None or not isinstance(span, dict)
                   or not 0 <= span.get("start", -1) < span.get("end", -1) <= len(source_text)
                   for span in spans):
                return False
            if any(source_text[span["start"]:span["end"]] != value
                   for value, span in zip(values, spans, strict=True)):
                return False
            if any(first["end"] > second["start"] for first, second in zip(spans, spans[1:])):
                return False
            if _compact(" ".join(values)) != _compact(source_text):
                return False
    return True
