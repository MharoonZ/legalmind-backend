"""Deterministic, legal-aware chunking of an approved corpus revision.

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

CHUNKER_VERSION = "legal-chunk-v1"

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
            result.chunks.append(DraftChunk(
                ordinal=ordinal_counter, root_provision_id=root.id, kind=root.kind,
                label=root.label, heading=root.heading, reference_path=root.reference_path,
                chunk_key=chunk_key, sequence_in_root=sequence, root_total_chunks=total,
                prefix=display_prefix, chunk_text=chunk_text, char_count=char_count,
                chunk_text_sha256=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                provision_ids=provision_ids,
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
