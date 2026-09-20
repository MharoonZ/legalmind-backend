"""Read structure out of the user's question before searching.

If someone writes "what does section 10 say", the section number is a far
better signal than any similarity score, so we look it up directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches: "section 10", "sections 10 and 11", "sec. 10", "s.10", "u/s 10",
# "§10", "section 2(h)" (the bracketed clause is ignored on purpose).
_SECTION_RE = re.compile(
    r"""
    (?:
        \bu\s*/\s*s\b            # u/s
      | \bsections?\b            # section / sections
      | \bsecs?\b\.?             # sec / secs / sec.
      | \bs\.\s*(?=\d)           # s. 10
      | §
    )
    \s*
    (?P<numbers>\d{1,3}(?:\s*(?:,|and|&|to|-|–)\s*\d{1,3})*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_NUMBER_RE = re.compile(r"\d{1,3}")
# "sections 5 to 8" should expand to 5, 6, 7, 8.
_RANGE_RE = re.compile(r"(\d{1,3})\s*(?:to|-|–)\s*(\d{1,3})", re.IGNORECASE)

MAX_DIRECT_SECTIONS = 3


@dataclass(frozen=True, slots=True)
class ParsedQuestion:
    text: str
    section_numbers: tuple[int, ...]
    out_of_range_sections: tuple[int, ...]

    @property
    def has_section_reference(self) -> bool:
        return bool(self.section_numbers)


def parse_question(
    question: str,
    *,
    section_min: int = 1,
    section_max: int = 30,
) -> ParsedQuestion:
    """Pull section numbers out of a question and split them by coverage.

    `section_numbers` are inside the corpus and safe to fetch directly.
    `out_of_range_sections` were asked for but are not in this corpus; the
    caller uses them to explain the refusal instead of silently ignoring them.
    """
    cleaned = " ".join(question.split())
    found: list[int] = []

    for match in _SECTION_RE.finditer(cleaned):
        fragment = match.group("numbers")
        numbers = [int(n) for n in _NUMBER_RE.findall(fragment)]
        for start, end in _RANGE_RE.findall(fragment):
            low, high = int(start), int(end)
            if low < high and high - low <= 10:
                numbers.extend(range(low, high + 1))
        found.extend(numbers)

    in_range, out_of_range = [], []
    for number in dict.fromkeys(found):  # de-duplicate, keep order
        target = in_range if section_min <= number <= section_max else out_of_range
        target.append(number)

    return ParsedQuestion(
        text=cleaned,
        section_numbers=tuple(sorted(in_range)[:MAX_DIRECT_SECTIONS]),
        out_of_range_sections=tuple(sorted(out_of_range)),
    )
