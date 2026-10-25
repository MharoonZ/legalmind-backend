"""Read-only reference lookups within one publication's approved structure.

Stored reviewed targets remain authoritative. Otherwise only a complete,
numbered, same-law source mention and a unique operative target are usable.
This is a structural lookup, not a declaration of legal effect or a DB edit.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_LABEL_TEXT = r"\d+(?:-[A-Za-z]+|[A-Za-z]+)?(?:\s*\(\s*[A-Za-z0-9]+\s*\))*"
_ROOT = re.compile(r"(?<![\w-])(?P<kind>sections?|rules?|articles?)\s+(?P<label>" + _LABEL_TEXT + r")", re.I)
_NEXT = re.compile(r"\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+)(?P<label>" + _LABEL_TEXT + r")", re.I)
_SUBSECTION = re.compile(r"\bsub[-\s]*section\s*\(\s*(\d+)\s*\)\s+of\s+(?:the\s+)?$", re.I)
_NESTED_PREFIX = re.compile(
    r"\b(?:sub[-\s]*sections?|paragraphs?|clauses?|sub[-\s]*clauses?)\s*\([A-Za-z0-9]+\)"
    r"[^.;:]{0,220}\bof\s+(?:(?:the|said|same)\s+)*$", re.I)
_SHORT_LIST = re.compile(r"\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+)\(\s*[A-Za-z0-9]+\s*\)", re.I)
_ADDRESS = re.compile(r"(?P<root>\d+(?:-[A-Za-z]+|[A-Za-z]+)?)(?P<children>(?:\([A-Za-z0-9]+\))*)$")
_RANGE = re.compile(r"\s*(?:to\b|through\b|[-\u2013\u2014])\s*\d", re.I)
_NON_OPERATIVE = {"omitted", "repealed", "deleted"}


def compact(value):
    return re.sub(r"\s+", "", value).casefold()


def _covers_reference(text, raw):
    candidate, prefix = compact(text), compact(raw)
    return bool(prefix and candidate.startswith(prefix) and
                (len(candidate) == len(prefix) or not candidate[len(prefix)].isalnum()))


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    kind: str
    labels: tuple[str, ...]
    text: str
    unsupported: bool = False
    source_start: int | None = None


@dataclass(frozen=True)
class ReferenceLookup:
    raw_reference: str
    target_instrument_id: object = None
    target_reference_path: str | None = None
    method: str = "stored"
    source_start: int | None = None
    source_end: int | None = None
    problem: str | None = None


def mentions(text):
    """Retain every member of a list; never treat its first number as the list."""
    result = []
    for match in _ROOT.finditer(text):
        end = match.end()
        labels = [compact(match["label"])]
        unsupported = False
        while continuation := _NEXT.match(text, end):
            labels.append(compact(continuation["label"]))
            end = continuation.end()
            if len(labels) > 64:
                unsupported = True
                break
        # Annotation markers such as 'section 3[59]' are not section 3.
        # Ranges need a reviewed interpretation, including inserted sections.
        unsupported |= bool(re.match(r"\s*\[", text[end:]) or _RANGE.match(text, end)
                            or _SHORT_LIST.match(text, end) or re.match(r"\s*&", text[end:]))
        start = match.start()
        prefix_start = max(0, start - 100)
        prefix = _SUBSECTION.search(text[prefix_start:start])
        nested_start = max(0, start - 300)
        nested = _NESTED_PREFIX.search(text[nested_start:start])
        if nested and (not prefix or nested_start + nested.start() != prefix_start + prefix.start()):
            unsupported = True
        if prefix:
            if len(labels) != 1 or "(" in labels[0]:
                unsupported = True
            else:
                labels[0] += f"({prefix[1]})"
        kind = match["kind"].casefold()
        kind = "section" if kind.startswith("s") else "rule" if kind.startswith("r") else "article"
        source_start = prefix_start + prefix.start() if prefix else start
        result.append(Mention(start, end, kind, tuple(labels), text[source_start:end], unsupported, source_start))
    return result


def _operative(node, catalog):
    seen = set()
    while node is not None:
        if node.id in seen or node.source_status in _NON_OPERATIVE:
            return False
        seen.add(node.id)
        if node.parent_id is None:
            return True
        parent = catalog.nodes.get(node.parent_id)
        if parent is None or parent.revision_id != node.revision_id:
            return False
        node = parent
    return False


def _external_scope(parts, title):
    scope = " ".join(parts).casefold()
    scope = re.sub(r"[^\w\s]", " ", scope)
    scope = " ".join(scope.split())
    # 'any act on the part of a person' denotes conduct, not a statute title.
    scope = re.sub(r"\bact\s+on\s+the\s+part\s+of\b", "conduct by", scope)
    scope = re.sub(r"\bfollowing\s+acts\s+are\s+declared\s+to\s+be\s+(?:prison\s+)?offences\b",
                   "following conduct offences", scope)
    scope = re.sub(r"\bany\s+regulation\s+of\s+the\s+prison\b"
                   r"(?=\s+(?:as\s+shall\s+have\s+been\s+)?declared\s+by\s+rules\b)",
                   "any institutional rule", scope)
    title = " ".join(re.findall(r"\w+", title.casefold())).removeprefix("the ")
    scope = scope.replace(title, " ")
    for noun in ("act", "rules", "code", "ordinance", "regulation", "regulations"):
        if re.search(r"\b" + noun + r"\b", title):
            scope = re.sub(r"\b(?:this|the|these)\s+" + noun + r"\b", " ", scope)
    return bool(
        re.search(r"(?<![a-z])\d*(?:acts?|ordinance|codes?|constitution|enactment|regulations?)\d*(?![a-z])", scope)
        or re.search(r"\b(?:other|another|said|same|that)\s+(?:law|statute)\b", scope)
        or re.search(r"\b(?:thereof|thereunder|aforementioned)\b", scope)
        or re.search(r"\bOrder\s*,?\s*\d{4}\b", " ".join(parts))
        or re.search(r"\b(?:of|under|in)\s+(?:(?:the|that|those|said|same)\s+)?rules\b", scope))


def _same_law(mention, node, item, catalog, groups):
    """Never use a same-number hit to resolve a mention of another law."""
    following = next((m.start for m in groups if m.start > mention.start), len(node.own_text))
    tail = node.own_text[mention.end:following]
    self_scope = re.match(r"\s*(?:of|in|under)\s+(this|the|these)\s+(Act|Rules|Code|Ordinance|Regulations?)\b", tail, re.I)
    if self_scope:
        # 'the Act' inside published Rules can mean their parent Act.
        noun = self_scope[2].casefold().rstrip("s")
        title_words = set(re.findall(r"\w+", item.instrument_title.casefold()))
        if not any(word.rstrip("s") == noun for word in title_words):
            return False
        # A coordinated external instrument is not an unambiguous self scope.
        rest = tail[self_scope.end():]
        if re.match(r"\s*(?:and|or)\s+(?:of\s+)?(?:the\s+)?\w.*\b(?:Act|Code|Rules|Ordinance)\b", rest, re.I | re.S):
            return False
        if self_scope[1].casefold() == "this":
            return True
    elif re.match(r"\s*(?:of|in|under)\s+", tail, re.I):
        return False
    # For an unqualified internal reference, exclude any competing named law
    # in this provision or its ancestors. This intentionally favours omission
    # over guessing when an external or contextual law is mentioned.
    parts, seen = [node.own_text], set()
    root = node if node.kind in {"section", "rule", "article"} else None
    current = node
    while current.parent_id is not None:
        if current.id in seen:
            return False
        seen.add(current.id)
        current = catalog.nodes.get(current.parent_id)
        if current is None or current.revision_id != node.revision_id:
            return False
        parts.append(current.own_text)
        if root is None and current.kind in {"section", "rule", "article"}:
            root = current
    # A sibling clause may supply the external-law context. Unqualified 'the
    # Act' or 'thereof' must not silently bind to the current Act instead.
    parent_parts = parts.copy()
    pending = list(catalog.children.get(root.id, ())) if root else []
    descendants = set()
    while pending:
        child = pending.pop()
        if child.id in descendants or child.revision_id != node.revision_id:
            return False
        descendants.add(child.id)
        parts.append(child.own_text)
        pending.extend(catalog.children.get(child.id, ()))
    # In an explicitly current-Act rule-making list, a bare reference in an
    # independent 'for ...' clause has local scope. A foreign-law clause keeps
    # its own scope, and a statement governing other clauses prevents fallback.
    if (self_scope is None and root is not None and node.parent_id == root.id and
            node.kind in {"subsection", "paragraph"} and node.label and
            re.search(r"\bmay\s+make\s+rules\s+consistent\s+with\s+this\s+Act\b", root.own_text, re.I) and
            re.match(r"\s*\([A-Za-z0-9]+\)\s+for\b", node.own_text, re.I) and
            not _external_scope(parent_parts, item.instrument_title)):
        governing = any(_external_scope([part], item.instrument_title) and re.search(
            r"\b(?:references|following|preceding|foregoing|throughout|above|below)\b", part, re.I)
            for part in parts[len(parent_parts):])
        if not governing:
            return True
    # A definition section can explicitly define 'Act' as this very instrument.
    # That source-backed declaration disambiguates 'the Act' in a sibling
    # definition even when another definition mentions a different statute.
    if (self_scope and self_scope[2].casefold() == "act" and root is not None and
            re.search(r"\b(?:definitions?|interpretation)\b", root.heading or "", re.I)):
        title = " ".join(re.findall(r"\w+", item.instrument_title.casefold())).removeprefix("the ")
        definition = re.compile(r"\bact\s+means\s+(?:the\s+)?" + re.escape(title) + r"\b")
        declared = any(definition.search(" ".join(re.findall(r"\w+", p.casefold()))) for p in parts)
        if declared and not _external_scope(parent_parts, item.instrument_title):
            return True
    return not _external_scope(parts, item.instrument_title)


def _target(kind, label, item, catalog):
    match = _ADDRESS.fullmatch(label)
    if match is None:
        return None
    roots = [n for n in catalog.nodes.values() if n.revision_id == item.revision_id
             and n.kind == kind and compact(n.label or "") == match["root"].casefold()]
    # Duplicate labels in different schedules/files must not be guessed away.
    if len(roots) != 1:
        return None
    target = roots[0]
    for child in re.findall(r"\(([^)]+)\)", match["children"]):
        children = [n for n in catalog.children.get(target.id, ())
                    if n.revision_id == item.revision_id and n.kind in {"subsection", "paragraph"}
                    and compact(n.label or "") == child.casefold()]
        if len(children) != 1:
            return None
        target = children[0]
    return target if _operative(target, catalog) else None


def reference_lookups(node, instrument_id, catalog):
    """Combine reviewed links with complete, source-backed structural lookups.

    Failed/rejected reviews are never overridden. Unmatched stored records
    remain unresolved. New list members are checked even if ingestion saved
    only the first member. Nothing here mutates the catalog or the database.
    """
    item = catalog.items[instrument_id]
    groups = mentions(node.own_text)
    stored = list(catalog.references.get(node.id, ()))
    matched = set()
    result = []
    for group in groups:
        raw = node.own_text[group.start:group.end]
        rows = [(index, row) for index, row in enumerate(stored)
                if getattr(row, "raw_reference", None) and
                _covers_reference(raw, row.raw_reference)]
        matched.update(index for index, _ in rows)
        # Without source offsets a repeated mention cannot be assigned a
        # possibly reviewed DB row unambiguously; keep it closed for review.
        repeated = any(other is not group and rows and _covers_reference(
            node.own_text[other.start:other.end], rows[0][1].raw_reference) for other in groups)
        blocked = group.unsupported or repeated or any(
            row.resolution_status not in {"unresolved", "resolved"} for _, row in rows)
        same_law = not blocked and _operative(node, catalog) and _same_law(group, node, item, catalog, groups)
        for number, label in enumerate(group.labels):
            # A legacy first-member row supplies only that member, never all
            # members of a printed list. Additional members need their own lookup.
            reviewed = [row for _, row in rows if row.resolution_status == "resolved"] if number == 0 else []
            if not blocked and len(reviewed) == 1:
                row = reviewed[0]
                explicit = _target(group.kind, label, item, catalog) if same_law else None
                conflict = explicit is not None and (
                    row.target_instrument_id != instrument_id or row.target_reference_path != explicit.reference_path)
                result.append(ReferenceLookup(group.text, row.target_instrument_id, row.target_reference_path,
                    problem="Stored target conflicts with the numbered same-law source mention." if conflict else None))
                continue
            target = _target(group.kind, label, item, catalog) if same_law and not reviewed else None
            result.append(ReferenceLookup(
                group.text, instrument_id if target else None, target.reference_path if target else None,
                method="same_revision_lookup", source_start=group.source_start, source_end=group.end,
                problem=None if target else "A complete, unambiguous operative same-law target could not be verified."))
    for index, row in enumerate(stored):
        if index in matched:
            continue
        resolved = row.resolution_status == "resolved"
        result.append(ReferenceLookup(
            getattr(row, "raw_reference", "") or "Stored source reference",
            getattr(row, "target_instrument_id", None), getattr(row, "target_reference_path", None),
            problem=None if resolved else "A stored source reference has no verified target."))
    return result
