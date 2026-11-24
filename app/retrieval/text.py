"""Text normalisation shared by the lexical index, law resolver and query parser.

Everything here is deterministic and corpus-agnostic: no law names, section
numbers or question-specific vocabulary are encoded in this module.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from threading import local

import snowballstemmer
from rapidfuzz.distance import OSA

_STEMMERS = local()
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SPACE = re.compile(r"\s+")

# Function words carry no retrieval signal. Legal operators such as "not",
# "shall" and "may" are deliberately kept out of this list.
STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be been being below between both but by can
could did do does doing down during each few for from further get give had has have having he her here
hers him his how i if in into is it its just kindly know let like me more most my need of off on once
only or other our ours out over own please same say says she so some such tell than that the their
theirs them then there these they this those through to too up us very want was we were what when where
which while who whom whose why will with would you your yours explain describe mean means meaning
""".split())


def normalize(value: str) -> str:
    """Unicode-compatibility form with collapsed whitespace; wording is unchanged."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def fold(value: str) -> str:
    """Case- and accent-insensitive form, used only for matching."""
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", value))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def words(value: str) -> list[str]:
    """Folded alphanumeric tokens in reading order."""
    return _WORD.findall(fold(value))


@lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    """English stem for ordinary words; identifiers and short tokens are kept."""
    if len(word) > 3 and word.isascii() and word.isalpha():
        if not hasattr(_STEMMERS, "english"):
            _STEMMERS.english = snowballstemmer.stemmer("english")
        return _STEMMERS.english.stemWord(word)
    return word


def content_words(value: str) -> list[str]:
    return [word for word in words(value) if word not in STOPWORDS]


def edit_budget(length: int) -> int:
    """How many single-character typos a word of this length may contain."""
    if length < 4:
        return 0
    return 1 if length < 8 else 2


def typo_distance(left: str, right: str) -> int | None:
    """Edit distance (adjacent transposition counts once) when within budget."""
    budget = edit_budget(min(len(left), len(right)))
    if budget == 0:
        return 0 if left == right else None
    distance = OSA.distance(left, right, score_cutoff=budget)
    return distance if distance <= budget else None


def compact_label(value: str | None) -> str:
    """Printed provision labels compared without spacing, hyphens or case: 5-A == 5a."""
    label = re.sub(r"[\s\-‐-―.]+", "", fold(value or ""))
    return label if label.isascii() else "".join(str(unicodedata.decimal(char, char)) for char in label)


# Statute PDFs print an amendment footnote number against inserted words:
# "1[three years]", "8[* * *]". The number is not part of the law's wording.
_FOOTNOTE_MARK = re.compile(r"(?<![\w.,])\d{1,3}(?=\[(?!\d+\]))")
_REFERENCE_WORD = re.compile(
    r"(?:sections?|sub-?sections?|rules?|articles?|clauses?|paragraphs?|schedules?|chapters?|parts?)\s*$", re.I)


def footnote_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges of amendment footnote numbers in extracted statute text.

    "section 3[59]" and "rule 5[A]" keep their number: there the digits are the
    provision being referred to, not a footnote.
    """
    return [match.span() for match in _FOOTNOTE_MARK.finditer(text)
            if not _REFERENCE_WORD.search(text, max(0, match.start() - 14), match.start())]


def without_footnote_marks(text: str) -> str:
    kept, cursor = [], 0
    for start, end in footnote_spans(text):
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    return "".join(kept)
