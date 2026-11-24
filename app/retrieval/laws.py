"""Resolve the law a user names against the laws that are actually published.

The resolver is built from the catalog of the active publication, so it works
for any set of laws without per-law rules. It tolerates misspelt, abbreviated,
reordered and partial titles ("Punjb Prevention of Gambling Ordinance",
"gambling ordinance", "Prison Act 1894", "PPGO 1978") and reports ambiguity or
an unknown law instead of silently guessing.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from rapidfuzz import process
from rapidfuzz.distance import OSA

from app.retrieval.text import edit_budget, fold, stem

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_YEAR = re.compile(r"(?:1[6-9]|20)\d\d")

# Words that say "this is a law" without identifying which one.
_TYPE_WORDS = {
    "act": "act", "acts": "act", "ordinance": "ordinance", "ordinances": "ordinance",
    "ord": "ordinance", "rules": "rules", "regulation": "regulation",
    "regulations": "regulation", "code": "code", "constitution": "constitution",
    "law": None, "laws": None, "statute": None,
}
_SENTENCE_BREAK = frozenset(".?!;:\n")
_FUZZY_TYPES = ("ordinance", "regulation", "regulations", "constitution")
_TITLE_STOP = frozenset({"the", "of", "and", "for", "in", "on", "to", "a", "an", "with", "by", "at"})
# A mention never extends across these: they belong to the question, not the title.
_BOUNDARY = frozenset({
    "section", "sections", "sec", "subsection", "rule", "article", "articles", "schedule", "clause",
    "under", "what", "which", "does", "do", "is", "are", "say", "says", "about", "explain", "tell",
    "me", "per", "from", "by", "according", "as", "this", "that", "same", "said", "any", "it",
    "how", "why", "when", "who", "can", "could", "would", "should", "will", "please", "define",
    "meaning", "means", "mean", "see", "read", "show", "give", "find",
})
_PREPOSITIONS = frozenset({"of", "under", "in", "per", "by", "from", "within"})

_MAX_WINDOW = 9
_BETA_SQUARED = 0.25           # precision of the mention counts more than title coverage
_ACCEPT_EXPLICIT = 0.55
_ACCEPT_LOOSE = 0.60
_ACCEPT_NAME = 0.50
_MIN_COVERAGE_EXPLICIT = 0.25
_MIN_COVERAGE_LOOSE = 0.40
_AMBIGUITY_MARGIN = 0.12
_MAX_CANDIDATES = 4


@dataclass(frozen=True, slots=True)
class CatalogLaw:
    instrument_id: UUID
    title: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LawMatch:
    instrument_id: UUID
    title: str
    score: float


@dataclass(frozen=True, slots=True)
class LawResolution:
    status: Literal["none", "resolved", "ambiguous", "unresolved"]
    matches: tuple[LawMatch, ...] = ()
    mention: str | None = None      # the user's own wording of the law
    explicit: bool = False          # a law-type word or year anchored the mention

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return tuple(match.instrument_id for match in self.matches)


@dataclass(frozen=True, slots=True)
class _Name:
    instrument_id: UUID
    title: str
    tokens: tuple[str, ...]
    types: frozenset[str]
    years: frozenset[str]
    acronyms: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Token:
    text: str        # folded
    raw: str         # as typed
    start: int
    end: int


def _type_of(word: str) -> tuple[bool, str | None]:
    if word in _TYPE_WORDS:
        return True, _TYPE_WORDS[word]
    if len(word) >= 7:
        for target in _FUZZY_TYPES:
            if abs(len(word) - len(target)) <= 2 and OSA.distance(word, target, score_cutoff=2) <= 2:
                return True, _TYPE_WORDS[target]
    return False, None


def _is_year(word: str) -> bool:
    return bool(_YEAR.fullmatch(word))


class LawResolver:
    def __init__(self, laws: Iterable[CatalogLaw]):
        self._laws = {law.instrument_id: law for law in laws}
        names: list[_Name] = []
        for law in self._laws.values():
            for text in dict.fromkeys((law.title, *law.aliases)):
                name = self._name(law, text)
                if name.tokens or name.years:
                    names.append(name)
        self._names = names
        frequency: dict[str, set[UUID]] = defaultdict(set)
        for name in names:
            for token in name.tokens:
                frequency[token].add(name.instrument_id)
        total = max(len(self._laws), 1)
        self._idf = {token: math.log(1.0 + total / len(owners)) for token, owners in frequency.items()}
        self._default_idf = math.log(1.0 + total)
        self._common = frozenset(token for token, owners in frequency.items()
                                 if len(owners) >= max(3, math.ceil(total * 0.1)))
        self._vocabulary = list(self._idf)
        self._by_stem: dict[str, list[str]] = defaultdict(list)
        for token in self._vocabulary:
            self._by_stem[stem(token)].append(token)
        self._names_by_token: dict[str, list[int]] = defaultdict(list)
        for index, name in enumerate(names):
            for token in set(name.tokens):
                self._names_by_token[token].append(index)

    @staticmethod
    def _name(law: CatalogLaw, text: str) -> _Name:
        raw = _TOKEN.findall(fold(text))
        tokens, types, years = [], set(), set()
        for position, word in enumerate(raw):
            if _is_year(word):
                years.add(word)
            elif word in _TYPE_WORDS and _TYPE_WORDS[word] and position:
                types.add(_TYPE_WORDS[word])
            elif word not in _TITLE_STOP:
                tokens.append(word)
        # Initialisms people actually type: with and without connecting words,
        # and with or without the leading jurisdiction word.
        ordered = [word for word in raw if not _is_year(word) and word != "the"]
        strict = [word for word in ordered if word not in _TITLE_STOP]
        acronyms = set()
        for sequence in (ordered, strict, strict[1:], ordered[1:]):
            untyped = sequence[:-1] if sequence and _TYPE_WORDS.get(sequence[-1]) else sequence
            for variant in (sequence, untyped):
                letters = "".join(word[0] for word in variant)
                if len(letters) >= 3:
                    acronyms.add(letters)
        return _Name(law.instrument_id, law.title, tuple(dict.fromkeys(tokens)),
                     frozenset(types), frozenset(years), frozenset(acronyms))

    @property
    def laws(self) -> tuple[CatalogLaw, ...]:
        return tuple(self._laws.values())

    def title(self, instrument_id: UUID) -> str:
        return self._laws[instrument_id].title

    # ------------------------------------------------------------ matching
    def _token_matches(self, word: str) -> dict[str, float]:
        """Catalog title words a typed word can stand for, with a quality in (0, 1]."""
        found: dict[str, float] = {}
        exact = word in self._idf
        if exact:
            found[word] = 1.0
        # A word typed exactly as it appears in one title is unlikely to be a
        # misspelling of a different title's word ("prisons" is not "prisoners").
        scale = 0.6 if exact else 1.0
        for token in self._by_stem.get(stem(word), ()):
            found.setdefault(token, 0.9 * scale)
        budget = edit_budget(len(word))
        if budget and word.isalpha():
            for token, distance, _ in process.extract(word, self._vocabulary, scorer=OSA.distance,
                                                      score_cutoff=budget, limit=6):
                if distance and distance <= edit_budget(min(len(word), len(token))):
                    found.setdefault(token, (0.8 if distance == 1 else 0.65) * scale)
        if len(word) >= 4 and not exact:
            for token in self._vocabulary:   # typed abbreviations: "prev" for "prevention"
                if len(token) > len(word) + 1 and token.startswith(word):
                    found.setdefault(token, 0.7)
        return found

    def _score(self, window: Sequence[str], *, year: str | None, law_type: str | None,
               matches: dict[str, dict[str, float]], loose: bool,
               min_coverage: float | None = None) -> list[tuple[float, float, _Name]]:
        candidates: set[int] = set()
        for word in window:
            for token in matches[word]:
                candidates.update(self._names_by_token.get(token, ()))
        scored = []
        for index in candidates:
            name = self._names[index]
            owned = set(name.tokens)
            quality: dict[str, float] = {}
            explained = total = 0.0
            for word in window:
                best_token, best = None, 0.0
                for token, value in matches[word].items():
                    if token in owned and value > best:
                        best_token, best = token, value
                known = bool(matches[word])
                weight = max((self._idf[token] for token in matches[word]), default=self._default_idf)
                if loose and not known:
                    continue        # an ordinary topic word says nothing about which law
                total += weight
                if best_token is not None:
                    explained += weight * best
                    quality[best_token] = max(quality.get(best_token, 0.0), best)
            if not quality or total == 0:
                continue
            precision = explained / total
            denominator = sum(self._idf[token] for token in name.tokens)
            coverage = sum(self._idf[token] * value for token, value in quality.items()) / denominator
            score = (1 + _BETA_SQUARED) * precision * coverage / (_BETA_SQUARED * precision + coverage)
            year_match = False
            if year and name.years:
                if year in name.years:
                    score, year_match = min(1.0, score + 0.15), True
                elif any(OSA.distance(year, own, score_cutoff=1) <= 1 for own in name.years):
                    score *= 0.9
                else:
                    score *= 0.6
            if law_type and name.types and law_type not in name.types:
                score *= 0.85
            minimum = min_coverage if min_coverage is not None else (
                _MIN_COVERAGE_LOOSE if loose else _MIN_COVERAGE_EXPLICIT)
            if coverage >= minimum or year_match:
                scored.append((score, coverage, name))
        return scored

    @staticmethod
    def _tokens(text: str) -> list[_Token]:
        folded = fold(text)
        if len(folded) != len(text):          # rare length-changing folds: lose casing only
            return [_Token(match[0], match[0], match.start(), match.end())
                    for match in _TOKEN.finditer(folded)]
        return [_Token(match[0], text[match.start():match.end()], match.start(), match.end())
                for match in _TOKEN.finditer(folded)]

    def resolve(self, text: str, *, loose: bool = False,
                exclude: Sequence[tuple[int, int]] = ()) -> LawResolution:
        """Find the named law in ``text``.

        ``loose`` also accepts scattered title words without a law-type word
        (used when a provision number is given but no law is named).
        ``exclude`` lists character ranges (for example parsed references)
        that are never part of a law name.
        """
        tokens = [token for token in self._tokens(text)
                  if not any(start <= token.start < end for start, end in exclude)]
        if not tokens:
            return LawResolution("none")
        cache: dict[str, dict[str, float]] = {}

        def matches_for(window):
            for word in window:
                if word not in cache:
                    cache[word] = self._token_matches(word)
            return cache

        best: dict[UUID, tuple[float, _Name]] = {}
        explicit = False
        unresolved_mention: str | None = None

        def keep(scored):
            for score, _, name in scored:
                if score > best.get(name.instrument_id, (0.0, None))[0]:
                    best[name.instrument_id] = (score, name)

        for position, token in enumerate(tokens):
            is_type, law_type = _type_of(token.text)
            following = tokens[position + 1].text if position + 1 < len(tokens) else ""
            if token.text == "order" and _is_year(following):
                is_type, law_type = True, "order"       # "... Order, 1984"
            if not is_type:
                continue
            year = next((other.text for other in tokens[position + 1:position + 3]
                         if _is_year(other.text)), None)
            window: list[_Token] = []
            cursor = position - 1
            while cursor >= 0 and len(window) < _MAX_WINDOW:
                current = tokens[cursor]
                if _SENTENCE_BREAK & set(text[current.end:tokens[cursor + 1].start]):
                    break               # a law name does not span sentences
                if _is_year(current.text):
                    year = year or current.text
                elif current.text.isdigit() or current.text in _BOUNDARY:
                    break
                else:
                    window.append(current)
                cursor -= 1
            window.reverse()
            content = [item for item in window if item.text not in _TITLE_STOP]
            forward = False
            if not content and following == "of":
                # "Code of Criminal Procedure": the name follows the type word.
                # Lower-case "an act of gambling" is ordinary English, not a title.
                named: list[_Token] = []
                for item in tokens[position + 2:position + 2 + _MAX_WINDOW]:
                    if (item.text.isdigit() and not _is_year(item.text)) or item.text in _BOUNDARY \
                            or _SENTENCE_BREAK & set(text[token.end:item.start]):
                        break
                    if _is_year(item.text):
                        year = year or item.text
                    elif item.text not in _TITLE_STOP:
                        named.append(item)
                if named and (year or any(item.raw[:1].isupper() for item in named)):
                    content, forward = named, True
            elif content:
                for follower in tokens[position + 1:position + 4]:
                    if follower.text in _TITLE_STOP or _is_year(follower.text):
                        continue
                    # Only words shared by many titles (a province or country
                    # name) are read as a trailing part of the law's name.
                    if follower.text in self._common:
                        content.append(follower)
                    break
            if not content:
                if year:        # "the 1978 ordinance"
                    hits = [name for name in self._names if year in name.years
                            and (not law_type or not name.types or law_type in name.types)]
                    if hits:
                        explicit = True
                        keep((0.7, 1.0, name) for name in hits)
                continue
            accepted = []
            for size in range(1, len(content) + 1):
                suffix = [item.text for item in content[-size:]]
                accepted.extend((score, coverage, name) for score, coverage, name in self._score(
                    suffix, year=year, law_type=law_type, matches=matches_for(suffix), loose=False)
                    if score >= _ACCEPT_EXPLICIT)
            if accepted:
                explicit = True
                keep(accepted)
                continue
            # A titled, dated or preposition-led phrase that matches nothing
            # published is a law the collection does not hold.
            titled = any(item.raw[:1].isupper() for item in content
                         if item.start > 0 and not _SENTENCE_BREAK & set(text[max(0, item.start - 2):item.start]))
            led = forward or (cursor >= 0 and tokens[cursor].text in _PREPOSITIONS and len(content) >= 2)
            if year or titled or led:
                first = min(content[0].start, token.start)
                last = max(content[-1].end, token.end)
                if _is_year(following):
                    last = max(last, tokens[position + 1].end)
                unresolved_mention = " ".join(text[first:last].split())

        if not best:
            for token in tokens:                # initialisms such as "PPGO 1978"
                if not 3 <= len(token.text) <= 10 or not token.text.isalpha():
                    continue
                year = next((other.text for other in tokens if _is_year(other.text)), None)
                if not (token.raw.isupper() or year):
                    continue
                hits = [name for name in self._names if token.text in name.acronyms
                        and (not year or not name.years or year in name.years)]
                if hits:
                    explicit = True
                    keep((0.9 if year else 0.8, 1.0, name) for name in hits)

        if not best and loose:
            window = [token.text for token in tokens
                      if token.text not in _TITLE_STOP and not token.text.isdigit()
                      and not _type_of(token.text)[0]]
            year = next((token.text for token in tokens if _is_year(token.text)), None)
            if window:
                keep((score, coverage, name) for score, coverage, name in self._score(
                    window, year=year, law_type=None, matches=matches_for(window), loose=True)
                    if score >= _ACCEPT_LOOSE)

        if not best:
            if unresolved_mention:
                return LawResolution("unresolved", mention=unresolved_mention, explicit=True)
            return LawResolution("none")
        ranked = sorted(best.values(), key=lambda item: (-item[0], item[1].title))
        top = ranked[0][0]
        near = [item for item in ranked if top - item[0] <= _AMBIGUITY_MARGIN][:_MAX_CANDIDATES]
        matches = tuple(LawMatch(name.instrument_id, self._laws[name.instrument_id].title, round(score, 4))
                        for score, name in near)
        return LawResolution("resolved" if len(matches) == 1 else "ambiguous", matches,
                             explicit=explicit)

    def resolve_name(self, value: str) -> LawResolution:
        """Resolve text that is known to be a law name (an API ``law`` hint,
        or the words after "section 3 of ...")."""
        resolved = self.resolve(value)
        if resolved.status in {"resolved", "ambiguous"}:
            return resolved
        loose = self.resolve(value, loose=True)
        if loose.status in {"resolved", "ambiguous"}:
            return loose
        # Every word is meant as part of the name, so a short name such as
        # "gambling" may match on a single distinctive title word.
        window = [token.text for token in self._tokens(value)
                  if token.text not in _TITLE_STOP and not token.text.isdigit()
                  and not _type_of(token.text)[0]]
        year = next((token.text for token in self._tokens(value) if _is_year(token.text)), None)
        if window:
            cache = {word: self._token_matches(word) for word in window}
            scored = [(score, name) for score, _, name in self._score(
                window, year=year, law_type=None, matches=cache, loose=False,
                min_coverage=_MIN_COVERAGE_EXPLICIT) if score >= _ACCEPT_NAME]
            if scored:
                best: dict[UUID, tuple[float, _Name]] = {}
                for score, name in scored:
                    if score > best.get(name.instrument_id, (0.0, None))[0]:
                        best[name.instrument_id] = (score, name)
                ranked = sorted(best.values(), key=lambda item: (-item[0], item[1].title))
                near = [item for item in ranked if ranked[0][0] - item[0] <= _AMBIGUITY_MARGIN][:_MAX_CANDIDATES]
                matches = tuple(LawMatch(name.instrument_id, self._laws[name.instrument_id].title,
                                         round(score, 4)) for score, name in near)
                return LawResolution("resolved" if len(matches) == 1 else "ambiguous", matches, explicit=True)
        return LawResolution("unresolved", mention=" ".join(value.split()), explicit=True)
