"""In-memory BM25 keyword index with typo-tolerant query expansion.

The index is built once per immutable publication. A query word that does not
occur in the published corpus is matched to the closest corpus words, so a
misspelt question still reaches the lexical channel.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence

import numpy as np
from rapidfuzz import process
from rapidfuzz.distance import OSA

from app.retrieval.text import STOPWORDS, edit_budget, stem, words

_K1 = 1.2
_B = 0.75
_REPAIR_WEIGHT = 0.7
_MAX_REPAIRS = 2
_MAX_REPAIR_LOOKUPS = 8


class LexicalIndex:
    def __init__(self, documents: Sequence[Sequence[tuple[str, float]]]):
        """``documents[i]`` is a list of ``(field_text, field_weight)`` pairs."""
        self.size = len(documents)
        postings: dict[str, dict[int, float]] = defaultdict(dict)
        lengths = np.zeros(self.size, dtype=np.float32)
        vocabulary: Counter[str] = Counter()
        for doc_id, fields in enumerate(documents):
            seen: set[str] = set()
            for text, weight in fields:
                for word in words(text):
                    if word in STOPWORDS:
                        continue
                    term = stem(word)
                    entry = postings[term]
                    entry[doc_id] = entry.get(doc_id, 0.0) + weight
                    lengths[doc_id] += weight
                    if word not in seen:
                        seen.add(word)
                        vocabulary[word] += 1
        self._lengths = lengths
        self._average = float(lengths.mean()) if self.size and lengths.any() else 1.0
        self._postings: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._idf: dict[str, float] = {}
        for term, entry in postings.items():
            ids = np.fromiter(entry.keys(), dtype=np.int32, count=len(entry))
            frequencies = np.fromiter(entry.values(), dtype=np.float32, count=len(entry))
            self._postings[term] = (ids, frequencies)
            self._idf[term] = float(np.log(1.0 + (self.size - len(entry) + 0.5) / (len(entry) + 0.5)))
        # Spelling repair uses alphabetic corpus words long enough to be distinctive.
        self._vocabulary = {word: count for word, count in vocabulary.items()
                            if len(word) >= 4 and word.isalpha()}
        self._vocabulary_words = list(self._vocabulary)
        self._known_words = set(vocabulary)

    def knows(self, word: str) -> bool:
        return word in self._known_words or stem(word) in self._postings

    def repair(self, word: str) -> list[str]:
        """Closest corpus spellings of an unknown word, best first."""
        budget = edit_budget(len(word))
        if budget == 0 or not word.isalpha() or not self._vocabulary_words:
            return []
        matches = process.extract(word, self._vocabulary_words, scorer=OSA.distance,
                                  score_cutoff=budget, limit=8)
        # For short words a single substitution often makes a different real word.
        # Keep missing/extra letters and adjacent transpositions in this length range.
        if len(word) < 8:
            matches = [item for item in matches if len(item[0]) != len(word)
                       or sorted(item[0]) == sorted(word)]
        ranked = sorted(matches, key=lambda item: (item[1], -self._vocabulary[item[0]], item[0]))
        if not ranked:
            return []
        best = ranked[0][1]
        return [candidate for candidate, distance, _ in ranked if distance == best][:_MAX_REPAIRS]

    def query_terms(self, text: str) -> tuple[list[tuple[str, float]], dict[str, str]]:
        """Weighted index terms for a question and the spelling repairs applied."""
        terms: dict[str, float] = {}
        repairs: dict[str, str] = {}
        lookups = 0
        for word in dict.fromkeys(words(text)):
            if word in STOPWORDS:
                continue
            term = stem(word)
            if term in self._postings:
                terms[term] = max(terms.get(term, 0.0), 1.0)
                continue
            if lookups >= _MAX_REPAIR_LOOKUPS or not word.isalpha() or len(word) < 4:
                continue
            lookups += 1
            candidates = self.repair(word)
            if candidates:
                repairs[word] = candidates[0]
            for candidate in candidates:
                repaired = stem(candidate)
                if repaired in self._postings:
                    terms[repaired] = max(terms.get(repaired, 0.0), _REPAIR_WEIGHT)
        return list(terms.items()), repairs

    def search(self, terms: Sequence[tuple[str, float]], *, mask: np.ndarray | None = None,
               limit: int = 50) -> list[tuple[int, float]]:
        """Top documents by BM25, optionally restricted to ``mask`` (bool per document)."""
        if not terms or not self.size:
            return []
        scores = np.zeros(self.size, dtype=np.float32)
        for term, weight in terms:
            posting = self._postings.get(term)
            if posting is None:
                continue
            ids, frequencies = posting
            norm = _K1 * (1.0 - _B + _B * self._lengths[ids] / self._average)
            scores[ids] += weight * self._idf[term] * frequencies * (_K1 + 1.0) / (frequencies + norm)
        if mask is not None:
            scores = np.where(mask, scores, 0.0)
        positive = np.flatnonzero(scores > 0)
        if positive.size == 0:
            return []
        if positive.size > limit:
            positive = positive[np.argpartition(-scores[positive], limit - 1)[:limit]]
        ordered = positive[np.lexsort((positive, -scores[positive]))]
        return [(int(doc_id), float(scores[doc_id])) for doc_id in ordered]
