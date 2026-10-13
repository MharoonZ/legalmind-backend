"""Deterministic, legal-aware chunking of an approved corpus revision.

The policy described below applies to the retained V1 builder. The
repository now calls ``build_chunks_from_spans`` (V2), which uses exact
source order to preserve parent text after child lists.

This module only reorganizes text that Phase A already parsed and stored in
``provisions``; it never rewrites or re-derives wording. A chunk's text is an
exact concatenation of one or more ``provisions.own_text`` values, in the
same document order the structure parser recorded via ``ordinal``.

Boundary policy (see docs/ABDULLAH_PHASE_B_HANDOVER.md section 7.1):

- One chunk per "root" provision (a Section/Rule/Article, a Schedule, or the
  Preamble) by default.
- A root whose combined text exceeds ``max_chars`` is split into more than
  one chunk, but a split only ever falls on a boundary between the root's
  direct children (e.g. between subsections) — never in the middle of a
  single provision's own text.
- ``other`` provisions (source notes, contents, running headers, ambiguous
  text) and any provision whose ``source_status`` marks it omitted, repealed,
  or deleted are excluded, along with their descendants.

This module has no database dependency and is intentionally pure/testable:
given a flat list of provisions for one revision, it returns the chunks to
persist. The repository layer is responsible for loading provisions and
writing the result idempotently.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

CHUNKER_VERSION = "legal-chunk-v2-source-order"

# A provision is never chunked as operative law if its kind is in this set,
# or if it carries one of these explicit source statuses (and neither are
# its descendants, below).
EXCLUDED_KINDS = frozenset({"other"})
EXCLUDED_SOURCE_STATUSES = frozenset({"omitted", "repealed", "deleted"})

# The provision kinds that anchor one chunk group each. Everything else
# (part/chapter/subsection/paragraph/preamble-children...) is gathered under
# the nearest enclosing root rather than chunked on its own.
ROOT_KINDS = frozenset({"preamble", "section", "rule", "article", "schedule"})

_KIND_WORD = {
    "section": "Section",
    "rule": "Rule",
    "article": "Article",
    "schedule": "Schedule",
}

DEFAULT_MAX_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ProvisionNode:
    """The subset of a stored ``Provision`` row the chunker needs.

    Decoupled from the ORM model so this module stays a pure function of
    plain data and is testable without a database.
    """

    id: UUID
    parent_id: UUID | None
    kind: str
    label: str | None
    reference_path: str
    ordinal: int
    heading: str | None
    own_text: str
    source_status: str | None


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """Pinned, versioned chunking configuration.

    ``config_sha256`` is part of a chunk set's idempotency key: changing any
    of these values is a deliberate re-chunk, producing a new chunk set
    rather than mutating a previous one (see handover section 7.1).
    """

    max_chars: int = DEFAULT_MAX_CHARS
    chunker_version: str = CHUNKER_VERSION

    def __post_init__(self) -> None:
        if self.max_chars < 1:
            raise ValueError("max_chars must be positive.")

    @property
    def config_sha256(self) -> str:
        payload = {
            "chunker_version": self.chunker_version,
            "max_chars": self.max_chars,
            "root_kinds": sorted(ROOT_KINDS),
            "excluded_kinds": sorted(EXCLUDED_KINDS),
            "excluded_source_statuses": sorted(EXCLUDED_SOURCE_STATUSES),
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DraftChunk:
    ordinal: int
    root_provision_id: UUID
    kind: str
    label: str | None
    heading: str | None
    reference_path: str
    chunk_key: str
    sequence_in_root: int
    root_total_chunks: int
    prefix: str
    chunk_text: str
    char_count: int
    chunk_text_sha256: str
    provision_ids: tuple[UUID, ...]
    search_text: str
    segments: tuple[DraftSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class ProvisionSourceSpan:
    id: UUID
    provision_id: UUID
    extraction_id: UUID
    page_index: int
    block_ordinal: int
    block_char_start: int
    provision_char_start: int
    provision_char_end: int
    mapping_quality: str


@dataclass(frozen=True, slots=True)
class DraftSegment:
    source_span_id: UUID
    provision_id: UUID
    provision_char_start: int
    provision_char_end: int
    chunk_char_start: int
    chunk_char_end: int


@dataclass(slots=True)
class ChunkBuildResult:
    chunks: list[DraftChunk] = field(default_factory=list)
    report: dict = field(default_factory=dict)


def build_prefix(instrument_title: str, root: ProvisionNode) -> str:
    """"Act title — section label — heading", generalized to every root kind."""
    title = instrument_title.strip()
    if root.kind == "preamble":
        parts = [title, "Preamble"]
        heading = (root.heading or "").strip()
        if heading and heading.casefold() != "preamble":
            parts.append(heading)
        return " — ".join(parts)
    word = _KIND_WORD[root.kind]
    label_part = f"{word} {root.label}" if root.label else word
    parts = [title, label_part]
    heading = (root.heading or "").strip()
    if heading:
        parts.append(heading)
    return " — ".join(parts)


def _root_label_part(root: ProvisionNode) -> str:
    """The bare "Section 19A" / "Rule 5" / "Preamble" part, no title or heading."""
    if root.kind == "preamble":
        return "Preamble"
    word = _KIND_WORD[root.kind]
    return f"{word} {root.label}" if root.label else word


def build_search_text(instrument_title: str, root: ProvisionNode, body: str) -> str:
    """Denormalized keyword-search text for one chunk: Act title, the root's
    own section/rule/article/schedule label, its heading, then the chunk's
    own body text — one per line so a GIN ``tsvector`` built from this field
    covers both exact statutory identifiers (e.g. ``19A``) and ordinary
    English content. This is deliberately independent of ``build_prefix``'s
    em-dash display formatting (and never carries a "(part X of Y)" suffix),
    so two different chunks of the same root (e.g. a long Section split in
    two) still each index the same stable "Section 19A" identifier rather
    than a continuation label that would blur exact lookups.

    A different label always produces different search text: ``label`` is
    inserted verbatim and unstemmed, so "Section 19" and "Section 19A" are
    never conflated by the tokenizer upstream of PostgreSQL's own lexer.
    """
    title = instrument_title.strip()
    label_part = _root_label_part(root)
    heading = (root.heading or "").strip()
    parts = [title, label_part]
    if heading:
        parts.append(heading)
    parts.append(body)
    return "\n".join(part for part in parts if part)


def _is_excluded(node: ProvisionNode) -> bool:
    return node.kind in EXCLUDED_KINDS or node.source_status in EXCLUDED_SOURCE_STATUSES


def _compute_excluded(provisions: list[ProvisionNode]) -> set[UUID]:
    """Exclude a node and, by propagation, every one of its descendants.

    Provisions are visited in ordinal order, which is document/parse order;
    since a parent is always created before its children in that order, a
    parent's exclusion is already decided by the time a child is checked.
    """
    excluded: set[UUID] = set()
    for node in sorted(provisions, key=lambda item: item.ordinal):
        if _is_excluded(node) or (node.parent_id is not None and node.parent_id in excluded):
            excluded.add(node.id)
    return excluded


def _direct_child_group(root: ProvisionNode, nodes_by_id: dict[UUID, ProvisionNode],
                         children_by_parent: dict[UUID | None, list[ProvisionNode]],
                         excluded: set[UUID]) -> dict[UUID, UUID]:
    """Map every included descendant of ``root`` to the direct child of
    ``root`` that owns it (its "group" for split purposes)."""
    owner: dict[UUID, UUID] = {}

    def visit(node: ProvisionNode, group_owner: UUID) -> None:
        for child in children_by_parent.get(node.id, []):
            if child.id in excluded or child.kind in ROOT_KINDS:
                # A nested root (should not occur in practice, but never
                # silently absorb another section/rule/schedule) stops the walk.
                continue
            owner[child.id] = group_owner
            visit(child, group_owner)

    for child in children_by_parent.get(root.id, []):
        if child.id in excluded or child.kind in ROOT_KINDS:
            continue
        owner[child.id] = child.id
        visit(child, child.id)
    return owner


def build_chunks(instrument_title: str, provisions: list[ProvisionNode],
                  config: ChunkConfig | None = None) -> ChunkBuildResult:
    config = config or ChunkConfig()
    result = ChunkBuildResult()
    warnings: list[dict] = []

    if not provisions:
        result.report = {
            "total_provisions": 0, "excluded_provisions": 0, "root_count": 0,
            "chunk_count": 0, "included_provisions": 0, "warnings": warnings,
        }
        return result

    nodes_by_id = {node.id: node for node in provisions}
    children_by_parent: dict[UUID | None, list[ProvisionNode]] = defaultdict(list)
    for node in provisions:
        children_by_parent[node.parent_id].append(node)
    for siblings in children_by_parent.values():
        siblings.sort(key=lambda item: item.ordinal)

    excluded = _compute_excluded(provisions)
    roots = sorted(
        (node for node in provisions if node.kind in ROOT_KINDS and node.id not in excluded),
        key=lambda item: item.ordinal,
    )

    included_provision_ids: set[UUID] = set()
    ordinal_counter = 0
    for root in roots:
        owner_by_id = _direct_child_group(root, nodes_by_id, children_by_parent, excluded)
        # Group 0 is the root's own text; subsequent groups are each direct
        # child's subtree, in document order.
        groups: dict[UUID, list[ProvisionNode]] = defaultdict(list)
        groups[root.id].append(root)
        for descendant_id, group_owner in owner_by_id.items():
            groups[group_owner].append(nodes_by_id[descendant_id])
        ordered_group_keys = [root.id] + sorted(
            (key for key in groups if key != root.id),
            key=lambda key: nodes_by_id[key].ordinal,
        )
        for key in ordered_group_keys:
            groups[key].sort(key=lambda item: item.ordinal)

        packed: list[list[ProvisionNode]] = []
        current: list[ProvisionNode] = []
        current_len = 0
        for key in ordered_group_keys:
            group_nodes = groups[key]
            group_len = sum(len(node.own_text.strip()) + 2
                            for node in group_nodes if node.own_text.strip())
            if current and current_len + group_len > config.max_chars:
                packed.append(current)
                current, current_len = [], 0
            current.extend(group_nodes)
            current_len += group_len
        if current:
            packed.append(current)

        packed = [group for group in packed
                  if any(node.own_text.strip() for node in group)]
        if not packed:
            warnings.append({"code": "empty_root_skipped",
                             "reference_path": root.reference_path,
                             "message": "Root provision had no non-blank text in itself "
                                        "or any included descendant; no chunk was created."})
            continue

        total = len(packed)
        prefix = build_prefix(instrument_title, root)
        for sequence, group_nodes in enumerate(packed, start=1):
            body = "\n\n".join(node.own_text.strip() for node in group_nodes
                               if node.own_text.strip())
            display_prefix = prefix if total == 1 else f"{prefix} (part {sequence} of {total})"
            chunk_text = f"{display_prefix}\n\n{body}"
            char_count = len(chunk_text)
            if char_count > config.max_chars and len(group_nodes) == 1:
                warnings.append({"code": "oversized_single_provision",
                                 "reference_path": group_nodes[0].reference_path,
                                 "char_count": char_count,
                                 "message": "A single provision's own text alone exceeds "
                                            "max_chars; kept whole rather than split mid-text."})
            chunk_key = (root.reference_path if total == 1
                         else f"{root.reference_path}/chunk:{sequence}")
            ordinal_counter += 1
            provision_ids = tuple(node.id for node in group_nodes)
            included_provision_ids.update(provision_ids)
            search_text = build_search_text(instrument_title, root, body)
            result.chunks.append(DraftChunk(
                ordinal=ordinal_counter, root_provision_id=root.id, kind=root.kind,
                label=root.label, heading=root.heading, reference_path=root.reference_path,
                chunk_key=chunk_key, sequence_in_root=sequence, root_total_chunks=total,
                prefix=display_prefix, chunk_text=chunk_text, char_count=char_count,
                chunk_text_sha256=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                provision_ids=provision_ids, search_text=search_text,
            ))

    result.report = {
        "total_provisions": len(provisions),
        "excluded_provisions": len(excluded),
        "root_count": len(roots),
        "chunk_count": len(result.chunks),
        "included_provisions": len(included_provision_ids),
        "warnings": warnings,
    }
    return result


@dataclass(frozen=True, slots=True)
class _Fragment:
    span: ProvisionSourceSpan
    node: ProvisionNode
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.node.own_text[self.start:self.end]


def build_chunks_from_spans(
    instrument_title: str,
    provisions: list[ProvisionNode],
    spans: list[ProvisionSourceSpan],
    config: ChunkConfig | None = None,
) -> ChunkBuildResult:
    """Build lossless chunks in *physical source order*, not parent-first order.

    Parent continuation after a child list is represented by a later source
    fragment of the same provision. Every fragment is tied to one exact Phase A
    span and stores its offsets in both the source provision and chunk text.
    Refuse missing/ambiguous provenance instead of silently publishing text in
    a guessed order. No Phase A row is modified.
    """
    config = config or ChunkConfig()
    nodes = {node.id: node for node in provisions}
    if len(nodes) != len(provisions):
        raise ValueError("Duplicate provision IDs in chunking input")
    excluded: set[UUID] = set()
    visiting: set[UUID] = set()

    def is_excluded(node: ProvisionNode) -> bool:
        if node.id in excluded:
            return True
        if node.id in visiting:
            raise ValueError("Cycle in provision parent hierarchy")
        visiting.add(node.id)
        parent = nodes.get(node.parent_id)
        value = _is_excluded(node) or (is_excluded(parent) if parent else False)
        visiting.remove(node.id)
        if value:
            excluded.add(node.id)
        return value

    for node in provisions:
        is_excluded(node)

    roots = {node.id: node for node in provisions
             if node.kind in ROOT_KINDS and node.id not in excluded}

    def root_for(node: ProvisionNode) -> UUID | None:
        seen = set()
        while node.id not in roots:
            if node.id in seen:
                raise ValueError("Cycle in provision parent hierarchy")
            seen.add(node.id)
            node = nodes.get(node.parent_id)
            if node is None:
                return None
        return node.id

    by_provision: dict[UUID, list[ProvisionSourceSpan]] = defaultdict(list)
    for span in spans:
        if span.provision_id in nodes:
            by_provision[span.provision_id].append(span)

    fragments_by_root: dict[UUID, list[_Fragment]] = defaultdict(list)
    included: set[UUID] = set()
    source_rank: dict[UUID, int] = {}
    for span in spans:
        node = nodes.get(span.provision_id)
        if node is not None:
            source_rank[span.extraction_id] = min(
                node.ordinal, source_rank.get(span.extraction_id, node.ordinal))

    for node in sorted(provisions, key=lambda item: item.ordinal):
        if node.id in excluded or node.kind in {"part", "chapter"} or not node.own_text.strip():
            continue
        root_id = root_for(node)
        if root_id is None:
            raise ValueError(f"Operative provision {node.reference_path} has no Section/Rule/Schedule root")
        ordered = sorted(by_provision[node.id], key=lambda item: item.provision_char_start)
        if not ordered:
            raise ValueError(f"No source spans for operative provision {node.reference_path}")
        previous_end = 0
        for index, span in enumerate(ordered):
            if span.mapping_quality != "exact" or span.block_ordinal < 1:
                raise ValueError(f"Non-exact source span for {node.reference_path}")
            if not 0 <= previous_end <= span.provision_char_start < span.provision_char_end <= len(node.own_text):
                raise ValueError(f"Overlapping/out-of-range source spans for {node.reference_path}")
            if node.own_text[previous_end:span.provision_char_start].strip():
                raise ValueError(f"Non-whitespace provision text has no source span: {node.reference_path}")
            end = span.provision_char_end
            if index == len(ordered) - 1:
                if node.own_text[end:].strip():
                    raise ValueError(f"Trailing provision text has no source span: {node.reference_path}")
                end = len(node.own_text)
            fragments_by_root[root_id].append(_Fragment(span, node, previous_end, end))
            previous_end = span.provision_char_end
        included.add(node.id)

    result = ChunkBuildResult()
    warnings: list[dict] = []
    ordinal = 0
    for root in sorted(roots.values(), key=lambda item: item.ordinal):
        extractions = {fragment.span.extraction_id for fragment in fragments_by_root[root.id]}
        ranks = [source_rank[extraction] for extraction in extractions]
        if len(ranks) != len(set(ranks)):
            raise ValueError(
                f"Ambiguous order of multiple PDF sources within {root.reference_path}; "
                "review source assignments before chunking")
        fragments = sorted(
            fragments_by_root[root.id],
            key=lambda f: (source_rank[f.span.extraction_id], str(f.span.extraction_id),
                           f.span.page_index, f.span.block_ordinal,
                           f.span.block_char_start, f.node.ordinal),
        )
        if not fragments:
            warnings.append({"code": "empty_root_skipped", "reference_path": root.reference_path})
            continue
        prefix = build_prefix(instrument_title, root)
        groups: list[list[_Fragment]] = []
        current: list[_Fragment] = []
        for fragment in fragments:
            projected = len(prefix) + 2 + sum(len(f.text) + 2 for f in current) + len(fragment.text)
            if current and projected > config.max_chars:
                groups.append(current)
                current = []
            current.append(fragment)
        if current:
            groups.append(current)
        total = len(groups)
        for sequence, group in enumerate(groups, start=1):
            display_prefix = prefix if total == 1 else f"{prefix} (part {sequence} of {total})"
            body_parts: list[str] = []
            segment_offsets: list[tuple[_Fragment, int, int]] = []
            cursor = len(display_prefix) + 2
            last: _Fragment | None = None
            for fragment in group:
                separator = "" if last and last.node.id == fragment.node.id and last.end == fragment.start else ("\n\n" if last else "")
                body_parts.append(separator)
                cursor += len(separator)
                body_parts.append(fragment.text)
                segment_offsets.append((fragment, cursor, cursor + len(fragment.text)))
                cursor += len(fragment.text)
                last = fragment
            body = "".join(body_parts)
            chunk_text = f"{display_prefix}\n\n{body}"
            if len(chunk_text) > config.max_chars:
                warnings.append({"code": "oversized_source_fragment", "reference_path": root.reference_path,
                                 "char_count": len(chunk_text)})
            ordinal += 1
            ids = tuple(dict.fromkeys(fragment.node.id for fragment in group))
            result.chunks.append(DraftChunk(
                ordinal=ordinal, root_provision_id=root.id, kind=root.kind,
                label=root.label, heading=root.heading, reference_path=root.reference_path,
                chunk_key=root.reference_path if total == 1 else f"{root.reference_path}/chunk:{sequence}",
                sequence_in_root=sequence, root_total_chunks=total,
                prefix=display_prefix, chunk_text=chunk_text, char_count=len(chunk_text),
                chunk_text_sha256=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                provision_ids=ids, search_text=build_search_text(instrument_title, root, body),
                segments=tuple(DraftSegment(
                    source_span_id=f.span.id, provision_id=f.node.id,
                    provision_char_start=f.start, provision_char_end=f.end,
                    chunk_char_start=start, chunk_char_end=end,
                ) for f, start, end in segment_offsets),
            ))
    result.report = {
        "format": "source_fragments_v2", "total_provisions": len(provisions),
        "excluded_provisions": len(excluded), "root_count": len(roots),
        "chunk_count": len(result.chunks), "included_provisions": len(included),
        "source_fragments": sum(len(chunk.segments) for chunk in result.chunks),
        "size_unit": "characters", "tokenizer": None,
        "warnings": warnings,
    }
    return result
