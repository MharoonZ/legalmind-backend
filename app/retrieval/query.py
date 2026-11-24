"""Understand a user message well enough to plan retrieval.

Deterministic and model-free: it reads provision references, resolves the law
the user names (or means, from the conversation), recognises follow-ups and
decides which text to search with. It never rejects a question; whatever it
cannot resolve is left for semantic search and the answer model.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from app.retrieval.laws import LawMatch, LawResolution
from app.retrieval.references import ParsedReferences, Reference, ReferenceRange, parse_references
from app.retrieval.snapshot import PublicationSnapshot
from app.retrieval.text import content_words, fold, normalize, stem, typo_distance, words

_MAX_SEARCH_CHARS = 2000
_GENERIC = frozenset({
    "section", "sections", "sec", "act", "acts", "law", "laws", "ordinance", "rule", "rules", "article",
    "articles", "schedule", "clause", "subsection", "provision", "provisions", "state", "states",
    "stated", "says", "said", "written", "read", "reads", "text", "full", "summary", "summarize",
    "under", "according", "cover", "covers", "regarding",
    "summarise", "details", "detail", "simple", "words", "briefly", "short", "kya", "hai", "ka", "ki",
})
_LEAD = re.compile(
    r"^(?:and|also|what about|how about|then|ok(?:ay)?\b[,\s]*(?:and|so|then)?|so|but|now|aur|phir)\b", re.I)
_DEIXIS = re.compile(
    r"\b(?:this|that|the same|said|above|aforesaid|these|those)\s+(?:act|law|ordinance|rules?|code|"
    r"sections?|provisions?|offen[cs]e|case|punishment|penalty|one)\b"
    r"|\b(?:it|its|them|they|thereof|therein|thereunder)\b", re.I)
_BARE_LAW = re.compile(r"\b(?:the|this|that|same)\s+(?:act|ordinance|law|code|rules)\b(?!\s+(?:of|on|for)\b)", re.I)
_RELATIONAL_TERMS = frozenset({
    "consequence", "consequences", "exception", "exceptions", "penalty", "penalties",
    "punishment", "remedy", "remedies",
})
_DEFINITION_WORDS = re.compile(
    r"\b(?:defin\w*|meaning of|meant by|interpret\w*)\b|\bwhat\s+(?:does|do)\b.{1,80}\bmeans?\b", re.I)
# "What is a common gaming house?", "who is a consumer under the Consumer Protection Act"
_DEFINITION_FORM = re.compile(
    r"^\s*(?:what|who)\s+(?:is|are|counts\s+as|qualifies\s+as)\s+(?:an?\s+|the\s+)?[\"\u201c']?"
    r"(?P<term>[\w-]+(?:\s+[\w-]+){0,3})[\"\u201d']?"
    r"(?:\s+(?:under|in|according\s+to|as\s+per|within|for\s+the\s+purposes?\s+of)\s+.{0,120}"
    r"\b(?:act|ordinance|law|rules|code)\b.{0,20})?\s*\??\s*$", re.I)
_NOT_A_TERM = frozenset({
    "for", "of", "to", "by", "from", "about", "when", "if", "punishment", "penalty", "penalties",
    "section", "sections", "rule", "article", "schedule", "difference", "procedure", "process",
    "fine", "sentence", "law", "act", "ordinance", "this", "that", "it", "allowed", "legal", "illegal",
})
_TRAILING_NAME = re.compile(
    r"\s*,?\s*(?:of|in|under|from|per)\s+(?:the\s+)?(?P<name>[^?.!;:\n]{2,90})", re.I)
_NAME_END = re.compile(
    r"\s+(?:say|says|state|states|mean|means|deal|deals|cover|covers|about|regarding|provide|provides|"
    r"talk|talks|is|are|was|and|or|for|when|if|please|in simple|kya|ka|ki)\b.*$", re.I)
_PRONOUN_NAME = re.compile(
    r"^(?:(?:this|that|the|same|said|above)\s+)*(?:act|ordinance|law|code|rules|it|one)?$", re.I)
_ORDINAL_REPLY = re.compile(
    r"^(?:the\s+)?(?:(?P<word>first|second|third|fourth|former|latter|last)|(?:option\s+|number\s+|no\.?\s*)?"
    r"(?P<digit>[1-9]))(?:st|nd|rd|th)?(?:\s+(?:one|option|law|act|ordinance))?[.!]?$", re.I)
_ORDINAL_INDEX = {"first": 0, "former": 0, "second": 1, "third": 2, "fourth": 3, "latter": -1, "last": -1}


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    question: str                               # the user's message, normalised
    search_text: str                            # what the retriever searches with
    references: tuple[Reference, ...] = ()
    ranges: tuple[ReferenceRange, ...] = ()
    law: LawResolution = field(default_factory=lambda: LawResolution("none"))
    law_source: Literal["request", "question", "context"] | None = None
    followup: bool = False
    context_used: bool = False
    wants_definition: bool = False
    topical_text: str = ""                      # the question without provision numbers
    jurisdiction: str | None = None

    @property
    def has_reference(self) -> bool:
        return bool(self.references or self.ranges)

    @property
    def scope(self) -> tuple[UUID, ...]:
        return self.law.instrument_ids if self.law.status in {"resolved", "ambiguous"} else ()

    @property
    def hard_scope(self) -> bool:
        """A law named in this message restricts search; a remembered one only biases it."""
        return bool(self.scope) and self.law_source in {"request", "question"}


def wants_definition(text: str) -> bool:
    """Whether the question asks what a term means, rather than what a rule provides."""
    if _DEFINITION_WORDS.search(text):
        return True
    match = _DEFINITION_FORM.match(text)
    return bool(match) and not _NOT_A_TERM & set(words(match["term"]))


def _without(text: str, spans: Sequence[tuple[int, int]]) -> str:
    kept, cursor = [], 0
    for start, end in sorted(spans):
        kept.append(text[cursor:start])
        cursor = max(cursor, end)
    kept.append(text[cursor:])
    return normalize(" ".join(kept))


def _topical(text: str) -> list[str]:
    return [word for word in content_words(text) if word not in _GENERIC and not word.isdigit()]


def _trailing_name(text: str, parsed: ParsedReferences) -> str | None:
    """"section 3 of gambling" names a law even without the word Act."""
    if not parsed.spans:
        return None
    match = _TRAILING_NAME.match(text, parsed.spans[-1][1])
    if not match:
        return None
    name = _NAME_END.sub("", match["name"]).strip(" ,'\"")
    if not name or _PRONOUN_NAME.fullmatch(name) or not content_words(name):
        return None
    return name


def _resolved(snapshot: PublicationSnapshot, instrument_id: UUID) -> LawResolution:
    return LawResolution("resolved", (LawMatch(instrument_id, snapshot.instruments[instrument_id].title, 1.0),),
                         explicit=True)


def _listed_laws(snapshot: PublicationSnapshot, text: str) -> list[UUID]:
    """Published laws named in a message, in the order they appear."""
    haystack = fold(text)
    found = []
    for instrument in snapshot.instruments.values():
        needle = fold(instrument.title).removeprefix("the ").strip()
        position = haystack.find(needle)
        if position >= 0:
            found.append((position, instrument.id))
    return [instrument_id for _, instrument_id in sorted(found, key=lambda item: item[0])]


def _in_jurisdiction(snapshot: PublicationSnapshot, resolution: LawResolution,
                     jurisdiction: str | None) -> LawResolution:
    if not jurisdiction or resolution.status not in {"resolved", "ambiguous"}:
        return resolution
    kept = tuple(match for match in resolution.matches
                 if snapshot.instruments[match.instrument_id].jurisdiction_code.casefold() == jurisdiction.casefold())
    if not kept:
        return LawResolution("unresolved", mention=resolution.matches[0].title, explicit=True)
    return LawResolution("resolved" if len(kept) == 1 else "ambiguous", kept,
                         mention=resolution.mention, explicit=resolution.explicit)


def analyze(snapshot: PublicationSnapshot, question: str, *, law_hint: str | None = None,
            instrument_id: UUID | None = None, jurisdiction: str | None = None,
            reference: Reference | None = None, previous_query: str | None = None,
            context_law: str | None = None, context_instrument_id: UUID | None = None,
            history: Sequence[tuple[str, str]] = ()) -> QueryAnalysis:
    """Plan retrieval for ``question``.

    ``history`` is ``(role, content)`` pairs, oldest first. ``previous_query``,
    ``context_law`` and ``context_instrument_id`` are the explicit follow-up
    fields of the API and take priority over inferred history.
    """
    text = normalize(question)
    parsed = ParsedReferences((reference,), (), ()) if reference else parse_references(text)
    resolver = snapshot.laws
    previous_users = [content for role, content in history if role == "user"]
    if previous_query:
        previous_users.append(previous_query)
    last_assistant = next((content for role, content in reversed(history) if role == "assistant"), None)
    previous = normalize(previous_users[-1]) if previous_users else None

    resolution, source = LawResolution("none"), None
    if instrument_id is not None:
        resolution = (_resolved(snapshot, instrument_id) if instrument_id in snapshot.instruments
                      else LawResolution("unresolved", mention=str(instrument_id), explicit=True))
        source = "request"
    elif law_hint:
        resolution, source = resolver.resolve_name(law_hint), "request"
    if resolution.status == "none":
        resolution = resolver.resolve(text, exclude=parsed.spans)
        if resolution.status == "none" and parsed:
            name = _trailing_name(text, parsed)
            resolution = resolver.resolve_name(name) if name else resolver.resolve(
                text, loose=True, exclude=parsed.spans)
        if resolution.status != "none":
            source = "question"

    merged_previous = False
    # "the second one" after the assistant listed laws to choose from.
    ordinal = _ORDINAL_REPLY.fullmatch(text)
    if ordinal and resolution.status == "none" and last_assistant:
        listed = _listed_laws(snapshot, last_assistant)
        index = _ORDINAL_INDEX[ordinal["word"].lower()] if ordinal["word"] else int(ordinal["digit"]) - 1
        if listed and -len(listed) <= index < len(listed):
            resolution, source, merged_previous = _resolved(snapshot, listed[index]), "question", True

    # A reply that only names a law answers "which law?" for the earlier question.
    if (not merged_previous and previous and not parsed and source == "question"
            and resolution.status in {"resolved", "ambiguous"} and len(words(text)) <= 10
            and not re.match(r"^(?:what|which|who|why|how|when|where|does|is|can|explain|describe)\b", text, re.I)):
        title_words = {word for match in resolution.matches for word in words(match.title)}
        extra = [word for word in _topical(text) if word not in title_words
                 and not any(word[:4] == known[:4] for known in title_words)]
        if len(extra) <= 1:
            merged_previous = True
    if merged_previous and previous:
        earlier = parse_references(previous)
        if earlier:
            parsed = earlier

    lead = bool(_LEAD.match(text))
    deixis = bool(_DEIXIS.search(text) or _BARE_LAW.search(text))
    topical = _topical(_without(text, parsed.spans))
    elliptical = len(topical) <= 2
    needs_previous = deixis or all(word in _RELATIONAL_TERMS for word in topical)
    followup = merged_previous or bool(previous_users or last_assistant) and (
        lead or deixis or (bool(parsed) and resolution.status == "none") or (elliptical and not parsed))

    if resolution.status == "none" and (followup or parsed) and (not parsed or needs_previous):
        carried = LawResolution("none")
        if context_instrument_id is not None and context_instrument_id in snapshot.instruments:
            carried = _resolved(snapshot, context_instrument_id)
        elif context_law:
            carried = resolver.resolve_name(context_law)
        if carried.status not in {"resolved", "ambiguous"}:
            turns = list(history) + ([("user", previous_query)] if previous_query else [])
            for _, content in reversed(turns):
                found = resolver.resolve(content)
                if found.status == "none" and parse_references(content):
                    found = resolver.resolve(content, loose=True)
                if found.status != "none":
                    carried = found
                    break
        # An explicitly named but unknown law is never replaced by a remembered one.
        if carried.status != "none":
            resolution, source = carried, "context"

    resolution = _in_jurisdiction(snapshot, resolution, jurisdiction)
    context_used = merged_previous or source == "context"
    search_text = text
    if previous and (merged_previous or (followup and source not in {"request", "question"} and needs_previous)):
        search_text = f"{previous}\n{text}"[-_MAX_SEARCH_CHARS:]
        context_used = True
    topical_text = _without(search_text if merged_previous else text, parse_references(
        search_text if merged_previous else text).spans)
    return QueryAnalysis(
        question=text, search_text=search_text, references=parsed.references, ranges=parsed.ranges,
        law=resolution, law_source=source, followup=followup, context_used=context_used,
        wants_definition=wants_definition(text), topical_text=topical_text,
        jurisdiction=jurisdiction)


def topic_words(analysis: QueryAnalysis, snapshot: PublicationSnapshot | None = None) -> list[str]:
    """Searchable words beyond a provision number and the named law's title or aliases."""
    topic = _topical(analysis.topical_text)
    if snapshot is not None and analysis.hard_scope:
        mention = analysis.law.mention
        if mention:
            return _topical(re.sub(re.escape(mention), " ", analysis.topical_text, count=1, flags=re.I))
        if analysis.law_source == "request":
            return topic
        titled = {word for key in analysis.scope for law in (snapshot.instruments[key],)
                  for name in (law.title, *law.aliases) for word in words(name)}
        stems = {stem(word) for word in titled}
        topic = [word for word in topic if stem(word) not in stems
                 and not any(typo_distance(word, known) is not None for known in titled if len(known) > 3)]
    return topic


def has_topic(analysis: QueryAnalysis, snapshot: PublicationSnapshot | None = None) -> bool:
    return bool(topic_words(analysis, snapshot))
