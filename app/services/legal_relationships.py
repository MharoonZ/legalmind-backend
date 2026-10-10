"""Conservative, source-anchored *proposals* for cross-instrument mentions.

No result from this module establishes legal effect, current applicability, or a
resolved target provision. A reviewer must inspect the source and target editions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from app.services.legal_structure import DraftNode, DraftSpan, ParsedStructure


_TITLE = re.compile(
    r"\b(?P<title>[Tt]he\s+[A-Z][A-Za-z'’()\-]*"
    r"(?:\s+(?:[A-Za-z][A-Za-z'’()\-]*|\([^)]{1,40}\))){0,12}"
    r"\s+(?:Act|Ordinance|Rules|Regulation),?\s+(?:18|19|20)\d{2})(?=\d{1,2}\b|\b)"
)
_REFERENCE = re.compile(
    r"\b(?:sections?\s+\d+[A-Za-z]*(?:\s*(?:to|[-–])\s*\d+[A-Za-z]*)?"
    r"|Chapter\s+[IVXLCDM]+)\b", re.IGNORECASE,
)
_CITATION = re.compile(r"\bAct\s+[IVXLCDM]+\s+of\s+(?:18|19|20)\d{2}\b", re.IGNORECASE)
_CROSS_LAW_KINDS = {"section", "subsection", "paragraph", "rule", "article", "schedule"}


def normalize_title(value: str) -> str:
    """Ignore title punctuation for self-reference filtering, not entity resolution."""
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


@dataclass(frozen=True, slots=True)
class RelationCandidateDraft:
    source_provision_id: UUID
    source_span: DraftSpan
    source_char_start: int
    source_char_end: int
    evidence_text: str
    target_title: str
    target_reference: str | None
    relation_kind: str
    link_basis: str


def _kind(node: DraftNode) -> str:
    lead = f"{node.heading or ''} {node.own_text[:300]}".casefold()
    # A title such as "The Public Gambling (Repeal) Act" is not itself an
    # operative repeal. Require a repeal verb or a repeal heading with an
    # explicit action in the body before proposing that legal effect.
    if re.search(r"\b(?:is|are|was|were|shall be|hereby)\s+(?:hereby\s+)?repealed\b", lead):
        return "repeals"
    if "amendment of" in lead or re.search(r"\bamends?\b", lead):
        return "amends"
    if "extension of" in lead or "shall extend" in lead:
        return "extends"
    return "cites"


def extract_relation_candidates(
    parsed: ParsedStructure, *, source_titles: set[str] | None = None,
) -> list[RelationCandidateDraft]:
    """Find explicit legal titles/references; retain only exact single-span evidence.

    Nearby section/chapter references are *candidate* associations to the next
    named Act in the same provision, never verified cross-law citations. An Act
    citation can use a definition in an earlier provision of the same draft.
    """
    spans_by_node: dict[UUID, list[DraftSpan]] = {}
    for span in parsed.spans:
        spans_by_node.setdefault(span.provision_id, []).append(span)

    output: list[RelationCandidateDraft] = []
    seen: set[tuple[UUID, int, int, str, str | None]] = set()
    defined_citations: dict[str, set[str]] = {}
    source_keys = {normalize_title(title) for title in (source_titles or set())}

    for node in sorted(parsed.nodes, key=lambda item: item.ordinal):
        if node.kind not in _CROSS_LAW_KINDS:
            continue
        source = node.own_text
        titles = [(match.start("title"), match.end("title"),
                   re.sub(r"\s+", " ", match.group("title")).strip())
                  for match in _TITLE.finditer(source)]
        relation_kind = _kind(node)

        def record(start: int, end: int, title: str,
                   target_reference: str | None, basis: str) -> None:
            if normalize_title(title) in source_keys:
                return
            evidence = source[start:end]
            if not evidence.strip():
                return
            source_span = next((span for span in spans_by_node.get(node.id, [])
                                if span.provision_char_start <= start
                                and end <= span.provision_char_end), None)
            if source_span is None:
                return
            key = (node.id, start, end, relation_kind, target_reference)
            if key in seen:
                return
            seen.add(key)
            output.append(RelationCandidateDraft(
                source_provision_id=node.id, source_span=source_span,
                source_char_start=start, source_char_end=end,
                evidence_text=evidence, target_title=title,
                target_reference=target_reference,
                relation_kind=relation_kind, link_basis=basis,
            ))

        for start, end, title in titles:
            record(start, end, title, None, "explicit_title")

        # A reference is associated only when the same provision names a law
        # shortly afterwards. "section 33 of this Act" is deliberately local.
        for reference in _REFERENCE.finditer(source):
            following = source[reference.end():reference.end() + 35]
            if re.match(r"\s+of\s+this\s+Act\b", following, re.IGNORECASE):
                continue
            target = next((title for title in titles
                           if 0 <= title[0] - reference.end() <= 350), None)
            if target is None:
                continue
            record(reference.start(), reference.end(), target[2],
                   re.sub(r"\s+", " ", reference.group(0)).strip(),
                   "nearby_reference")

        # A printed short citation such as "Act II of 1947" may be defined by
        # an explicit title in a previous section. Keep the association as a
        # proposal; do not treat it as a verified identity or legal effect.
        citations = list(_CITATION.finditer(source))
        if relation_kind in {"extends", "amends", "repeals"} and not titles:
            for citation in citations:
                known = defined_citations.get(normalize_title(citation.group(0)), set())
                if len(known) == 1:
                    record(citation.start(), citation.end(), next(iter(known)),
                           None, "defined_short_citation")
        if len({normalize_title(title[2]) for title in titles}) == 1:
            for citation in citations:
                defined_citations.setdefault(normalize_title(citation.group(0)), set()).add(titles[0][2])

    return output
