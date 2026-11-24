"""Immutable in-memory view of one active corpus publication.

A publication is sealed and write-protected in PostgreSQL, so its text,
structure, source spans and vectors can be read once and served from memory.
Every search and citation then works without a database round trip, and a
snapshot is replaced as a whole when another publication is activated.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import numpy as np

from app.embeddings.profiles import EmbeddingProfile
from app.retrieval.laws import CatalogLaw, LawResolver
from app.retrieval.lexical import LexicalIndex
from app.retrieval.references import schedule_number
from app.retrieval.text import compact_label, stem, without_footnote_marks, words

_NON_OPERATIVE = frozenset({"omitted", "repealed", "deleted"})
_ROOT_KINDS = ("section", "rule", "article", "schedule")
_SPACE = re.compile(r"\s+")
_DEFINITION_HEADING = re.compile(r"\b(?:definitions?|interpretation)\b", re.I)
_DEFINED_TERM = re.compile(
    r"[\"“]([^\"”]{2,80})[\"”]\s*(?:means|includes|shall mean|has the same meaning|refers to)", re.I)
_HEADING_WEIGHT = 2.5


class SnapshotError(RuntimeError):
    """The active publication cannot be served as a complete, consistent snapshot."""


@dataclass(frozen=True, slots=True)
class Instrument:
    id: UUID
    title: str
    citation_key: str
    aliases: tuple[str, ...]
    instrument_type: str
    jurisdiction_code: str
    legal_version_id: UUID
    edition_key: str
    applicability_status: str
    revision_id: UUID
    chunk_set_id: UUID


@dataclass(frozen=True, slots=True)
class Segment:
    """Exact mapping of a range of chunk text to a provision and a PDF page."""
    start: int
    end: int
    provision_id: UUID
    kind: str
    label: str | None
    reference_path: str | None
    source_span_id: UUID
    file_id: UUID
    file_sha256: str
    filename: str
    source_url: str | None
    page_index: int
    extras: Mapping[str, Any] = field(default_factory=dict)

    def as_source_ref(self) -> dict[str, Any]:
        return {
            "provision_id": self.provision_id, "source_span_id": self.source_span_id,
            "file_id": self.file_id, "file_sha256": self.file_sha256, "filename": self.filename,
            "source_url": self.source_url, "reference_path": self.reference_path,
            "kind": self.kind, "label": self.label, "page_index": self.page_index,
            "chunk_char_start": self.start, "chunk_char_end": self.end, **self.extras,
        }


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int                    # row in PublicationSnapshot.vectors
    id: UUID
    instrument_id: UUID
    root_provision_id: UUID
    ordinal: int
    kind: str
    label: str | None
    heading: str | None
    reference_path: str
    sequence_in_root: int
    root_total_chunks: int
    prefix: str                   # display heading; not statutory wording
    text: str                     # exact published chunk text (prefix + source text)
    segments: tuple[Segment, ...]

    @property
    def body_start(self) -> int:
        return len(self.prefix) + 2 if self.text.startswith(self.prefix) else 0

    @property
    def citation(self) -> str:
        """How a reader names this provision: "Section 5-A", "First Schedule"."""
        if self.kind == "preamble":
            return "Preamble"
        if self.kind == "schedule":
            return f"{self.label.title()} Schedule" if self.label else "Schedule"
        return f"{self.kind.capitalize()} {self.label}" if self.label else self.kind.capitalize()


@dataclass(frozen=True, slots=True)
class ProvisionNode:
    id: UUID
    instrument_id: UUID
    parent_id: UUID | None
    kind: str
    label: str | None
    heading: str | None
    reference_path: str | None
    ordinal: int
    text: str
    source_status: str | None


def readable(text: str) -> str:
    """Model-facing rendering: PDF line wrapping and footnote numbers removed."""
    return _SPACE.sub(" ", without_footnote_marks(text)).strip()


class PublicationSnapshot:
    def __init__(self, *, publication_id: UUID, name: str, manifest_sha256: str,
                 profile: EmbeddingProfile, instruments: Iterable[Instrument],
                 chunks: Sequence[Chunk], vectors: np.ndarray, nodes: Iterable[ProvisionNode]):
        self.id = publication_id
        self.name = name
        self.manifest_sha256 = manifest_sha256
        self.profile = profile
        self.instruments: dict[UUID, Instrument] = {item.id: item for item in instruments}
        self.chunks: tuple[Chunk, ...] = tuple(chunks)
        self.nodes: dict[UUID, ProvisionNode] = {node.id: node for node in nodes}
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape != (len(self.chunks), profile.dimension):
            raise SnapshotError("Published vectors do not match the published chunks.")
        if len(self.chunks) and not np.isfinite(vectors).all():
            raise SnapshotError("A published vector is not finite.")
        if any(chunk.index != position for position, chunk in enumerate(self.chunks)):
            raise SnapshotError("Published chunks are not in index order.")
        if any(chunk.instrument_id not in self.instruments for chunk in self.chunks):
            raise SnapshotError("A published chunk belongs to an unpublished law.")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if len(self.chunks) and (norms == 0).any():
            raise SnapshotError("A published vector is zero.")
        self.vectors = vectors / np.where(norms == 0, 1.0, norms)
        self.vectors.setflags(write=False)
        self._build_indexes()

    # ------------------------------------------------------------- indexes
    def _build_indexes(self) -> None:
        self.chunk_by_id = {chunk.id: chunk for chunk in self.chunks}
        roots: dict[tuple[UUID, UUID], list[Chunk]] = defaultdict(list)
        for chunk in self.chunks:
            roots[(chunk.instrument_id, chunk.root_provision_id)].append(chunk)
        self.chunks_by_root = {key: tuple(sorted(parts, key=lambda c: c.sequence_in_root))
                               for key, parts in roots.items()}
        self.root_order: dict[UUID, list[tuple[UUID, UUID]]] = defaultdict(list)
        self._labels: dict[tuple[UUID, str], list[tuple[str, UUID]]] = defaultdict(list)
        for key, parts in sorted(self.chunks_by_root.items(), key=lambda item: item[1][0].ordinal):
            first = parts[0]
            self.root_order[first.instrument_id].append(key)
            label = schedule_number(first.label) if first.kind == "schedule" else compact_label(first.label)
            self._labels[(first.instrument_id, label)].append((first.kind, first.root_provision_id))
        children: dict[UUID, list[ProvisionNode]] = defaultdict(list)
        for node in self.nodes.values():
            if node.parent_id is not None:
                children[node.parent_id].append(node)
        self.children = {key: tuple(sorted(rows, key=lambda n: n.ordinal)) for key, rows in children.items()}
        masks: dict[UUID, np.ndarray] = {}
        for instrument_id in self.instruments:
            masks[instrument_id] = np.fromiter(
                (chunk.instrument_id == instrument_id for chunk in self.chunks), dtype=bool,
                count=len(self.chunks))
        self._masks = masks
        self.display = tuple(readable(chunk.text[chunk.body_start:]) for chunk in self.chunks)
        self.lexical = LexicalIndex([
            [(self.instruments[chunk.instrument_id].title, 1.0),
             (chunk.citation, 1.0),
             (chunk.heading or "", _HEADING_WEIGHT),
             (self.display[chunk.index], 1.0)]
            for chunk in self.chunks])
        self.laws = LawResolver(
            CatalogLaw(item.id, item.title, tuple(alias for alias in item.aliases if alias))
            for item in self.instruments.values())
        self.definitions: dict[UUID, tuple[tuple[UUID, UUID], frozenset[tuple[str, ...]]]] = {}
        for instrument_id, keys in self.root_order.items():
            for key in keys:
                first = self.chunks_by_root[key][0]
                if first.kind != "schedule" and _DEFINITION_HEADING.search(first.heading or ""):
                    text = " ".join(self.display[part.index] for part in self.chunks_by_root[key])
                    terms = frozenset(tuple(stem(word) for word in words(term))
                                      for term in _DEFINED_TERM.findall(text))
                    self.definitions[instrument_id] = (key, frozenset(term for term in terms if term))
                    break

    # -------------------------------------------------------------- lookup
    def mask(self, instrument_ids: Iterable[UUID] | None) -> np.ndarray | None:
        if instrument_ids is None:
            return None
        combined = np.zeros(len(self.chunks), dtype=bool)
        for instrument_id in instrument_ids:
            if instrument_id in self._masks:
                combined |= self._masks[instrument_id]
        return combined

    def find_root(self, instrument_id: UUID, kind: str, label: str) -> tuple[UUID, UUID] | None:
        """The root provision a printed label names within one law.

        The stated kind is preferred, but "article 5" of an Act that only has
        sections still finds section 5: users mix these words freely.
        """
        rows = self._labels.get((instrument_id, label), ())
        if not rows:
            return None
        preferred = [root for found_kind, root in rows if found_kind == kind]
        if len(preferred) == 1:
            return instrument_id, preferred[0]
        if not preferred:
            alternatives = [root for found_kind, root in rows
                            if (found_kind == "schedule") == (kind == "schedule")]
            if len(alternatives) == 1:
                return instrument_id, alternatives[0]
        elif preferred:
            return instrument_id, preferred[0]      # duplicated label: first in document order
        return None

    def roots_between(self, instrument_id: UUID, kind: str, start: str, end: str) -> list[tuple[UUID, UUID]]:
        """Every root from ``start`` to ``end`` in document order ("sections 3 to 5")."""
        first, last = self.find_root(instrument_id, kind, start), self.find_root(instrument_id, kind, end)
        if first is None or last is None:
            return []
        order = self.root_order[instrument_id]
        low, high = sorted((order.index(first), order.index(last)))
        return [key for key in order[low:high + 1]
                if self.chunks_by_root[key][0].kind == self.chunks_by_root[first][0].kind]

    def child_path(self, root_id: UUID, labels: Sequence[str]) -> str | None:
        """Reference path of ``root(2)(a)`` when those children exist."""
        node = self.nodes.get(root_id)
        for label in labels:
            if node is None:
                return None
            node = next((child for child in self.children.get(node.id, ())
                         if child.source_status not in _NON_OPERATIVE
                         and compact_label((child.label or "").strip("()")) == compact_label(label)), None)
        return node.reference_path if node else None

    def labels_of(self, instrument_id: UUID, kind: str = "section") -> list[str]:
        return [self.chunks_by_root[key][0].label or "" for key in self.root_order.get(instrument_id, ())
                if self.chunks_by_root[key][0].kind == kind]

    def breadcrumbs(self, root_id: UUID) -> tuple[str, ...]:
        """Enclosing Part/Chapter headings, outermost first."""
        trail: list[str] = []
        seen: set[UUID] = set()
        node = self.nodes.get(root_id)
        while node is not None and node.parent_id is not None and node.parent_id not in seen:
            seen.add(node.parent_id)
            node = self.nodes.get(node.parent_id)
            if node is not None and node.kind in {"part", "chapter"}:
                title = " ".join(filter(None, (node.kind.capitalize(), node.label, node.heading)))
                trail.append(_SPACE.sub(" ", title).strip())
        return tuple(reversed(trail))

    def describe(self) -> dict[str, Any]:
        return {"publication_id": str(self.id), "name": self.name, "laws": len(self.instruments),
                "chunks": len(self.chunks), "embedding_model": self.profile.model_id}
