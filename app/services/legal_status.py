"""Conservative labels for what a printed source explicitly marks non-operative.

These labels describe the source PDF's rendition, not verified current law.
Never infer a repeal merely from an operative clause about another enactment.
"""

from __future__ import annotations

import re


LEGAL_KINDS = {
    "section", "subsection", "paragraph", "article", "rule",
    "part", "chapter", "schedule",
}
SOURCE_STATUSES = {"repealed", "omitted", "deleted"}
_DIRECT = re.compile(
    r"(?:\A|[.\]\n])\s*[-\u2013\u2014\ufffd]?\s*\[?\s*"
    r"(?P<status>repealed|omitted|deleted)\b"
    r"(?=\s+by\b|\s*[.\]])",
    re.IGNORECASE,
)
_NOTE_OPENING = re.compile(
    r"^\s*\d{1,3}\s*"
    r"(?P<following>The\s+following\s+)?"
    r"(?:(?P<kind>sub[- ]?section|section|paragraph|clause|part|chapter|schedule)"
    r"\s*(?P<label>\([A-Za-z0-9-]+\)|\d+[A-Za-z-]*|[IVXLCDM]+)\s*)?"
    r"(?:was\s+|is\s+)?"
    r"(?P<status>repealed|omitted|deleted)\s+by\b",
    re.IGNORECASE,
)


def direct_source_status(kind: str, text: str) -> str | None:
    """Classify a status clause adjoining the node's heading, not legal prose."""
    if kind not in LEGAL_KINDS:
        return None
    for match in _DIRECT.finditer(text[:400]):
        prefix = text[:match.start()]
        # A substantive provision before the phrase makes its scope uncertain.
        if len(prefix) > 300 or re.search(r"\b(?:shall|may|hereby)\b", prefix, re.I):
            continue
        return match.group("status").casefold()
    return None


def is_star_only(text: str, label: str | None) -> bool:
    """Printed identifier/footnote marker plus stars, with no operative words."""
    if "*" not in text:
        return False
    remainder = re.sub(r"[\d\s*\[\]().;:,_\-\u2013\u2014\ufffd]", "", text)
    letters = re.sub(r"[^A-Za-z]", "", label or "")
    if letters:
        remainder = re.sub(re.escape(letters), "", remainder, count=1, flags=re.I)
    return not remainder


def linked_note_status(note_text: str, *, target_kind: str,
                       target_label: str | None) -> tuple[str | None, str | None]:
    """Use only a unique marker link and a note clearly naming that target.

    Returns (status, reason). A nonempty reason asks for review; no guess is made.
    """
    match = _NOTE_OPENING.match(note_text)
    if match is None:
        return None, "note_does_not_explicitly_state_status"
    status = match.group("status").casefold()
    kind = match.group("kind")
    label = match.group("label")
    if kind:
        normalized_kind = kind.casefold().replace("-", "").replace(" ", "")
        if normalized_kind == "clause":
            normalized_kind = "paragraph"
        if normalized_kind != target_kind:
            return None, "note_names_different_kind"
        if (label or "").strip("()").casefold() != (target_label or "").casefold():
            return None, "note_names_different_identifier"
    elif match.group("following"):
        # "The following omitted by ..." is safe only if the note also quotes
        # the same printed identifier; otherwise the referent is uncertain.
        if not target_label or not re.search(
            rf"(?m)(?:^|[:\n\"'\u201c\u2018\ufffd])\s*"
            rf"{re.escape(target_label)}\.(?=\s)",
            note_text[match.end():], re.I,
        ):
            return None, "following_note_without_matching_identifier"
    return status, None
