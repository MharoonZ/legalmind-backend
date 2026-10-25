"""Conservative scopes for explicit first-level subsection reference targets."""
from dataclasses import dataclass
import re

from app.services.qa_references import mentions, reference_lookups


_QUALIFIER = re.compile(
    r"\b(?:provisos?|provided|explanations?|notwithstanding|unless|except(?:ions?)?|"
    r"exempt\w*|nothing|restrict\w*|limit\w*|subject\s+to|delegat\w*|"
    r"only|requir\w*|while|appeals?|review|waiv\w*|suspend\w*|modif\w*|amend\w*|"
    r"extend\w*|alter\w*|reduc\w*|vary|"
    r"shall\s+not|may\s+not|does\s+not|this\s+section|"
    r"no\s+(?:\w+\s+){0,5}(?:shall|may|be|required))\b", re.I)
_LOCAL_KIND = re.compile(r"\b(?:sub[-\s]*sections?|paragraphs?|clauses?)\b", re.I)
_LOCAL_LABELS = re.compile(
    r"\s*(?P<label>\(\s*[A-Za-z0-9]+\s*\)|[A-Za-z0-9]+)"
    r"(?P<more>(?:\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+)"
    r"\(\s*[A-Za-z0-9]+\s*\))*)", re.I)


@dataclass(frozen=True)
class ReferenceScope:
    target_id: object
    related: frozenset
    qualifications: frozenset


def _words(value):
    return re.findall(r"\w+", value.casefold())


def nested_reference_scope(target, root, catalog):
    """Return a complete branch group, or None to retain the full-section path.

    Only a direct numbered child below a title-only powers section can be
    narrowed. Parent text, complete selected branches, conservative qualifiers
    and intra-section dependencies in either direction remain in the group.
    Ambiguous hierarchy/selector syntax always falls back to the full root.
    """
    if (target.parent_id != root.id or target.kind not in {"subsection", "paragraph"} or
            root.kind not in {"section", "rule", "article"} or not target.label or not root.heading or
            not re.search(r"\bpowers?\b", root.heading, re.I) or
            _words(root.own_text) != _words(f"{root.label or ''} {root.heading}")):
        return None
    branches = list(catalog.children.get(root.id, ()))
    if not branches or target.id not in {n.id for n in branches}:
        return None
    items = [item for item in catalog.items.values() if item.revision_id == root.revision_id]
    if len(items) != 1:
        return None
    instrument_id = items[0].instrument_id
    labels, nodes, references, plans, qualifying = {}, {}, {}, {}, set()
    for branch in branches:
        if branch.revision_id != root.revision_id:
            return None
        if branch.kind not in {"subsection", "paragraph"} and branch.heading not in {"Proviso", "Explanation"}:
            return None
        if branch.kind in {"subsection", "paragraph"} and branch.label:
            label = branch.label.strip().strip('()').casefold()
            if not label.isalnum() or label in labels:
                return None
            labels[label] = branch.id
        elif branch.kind in {"subsection", "paragraph"}:
            return None
        pending, group = [branch], {}
        while pending:
            node = pending.pop()
            if node.id in group or node.revision_id != root.revision_id:
                return None
            group[node.id] = node
            pending.extend(catalog.children.get(node.id, ()))
        nodes[branch.id] = group
        refs = set()
        plans[branch.id] = []
        # Only clearly separate permissive powers may be left out. Ordinary
        # conditions and uncertain prose remain, even without a keyword hit.
        words = _words(branch.own_text)
        if words and words[0] == (branch.label or '').strip('() ').casefold():
            words = words[1:]
        body = " ".join(words)
        independent_power = re.match(r"(?:[a-z]+\s+){1,16}may\s+(?!not\b)", body)
        if not independent_power or re.match(
                r"(?:where|when|if|upon|until|during|after|before|despite|however|otherwise|"
                r"it|they|he|she|this|that|these|those|such)\b", body):
            qualifying.add(branch.id)
        for node in group.values():
            if node.heading in {"Proviso", "Explanation"} or _QUALIFIER.search(node.own_text):
                qualifying.add(branch.id)
            for mention in _LOCAL_KIND.finditer(node.own_text):
                numbered = [n for n in branches if n.kind in {"subsection", "paragraph"}]
                kind = "subsection" if mention[0].casefold().startswith('sub') else "paragraph"
                if any(n.kind != kind for n in numbered):
                    return None
                tail = node.own_text[mention.end():]
                match = _LOCAL_LABELS.match(tail)
                if match is None or not match['label'].strip('() ').isalnum():
                    return None
                # An explicit outer section/law, range or compound selector is
                # not an unambiguous reference to a sibling of this root.
                rest = tail[match.end():]
                if re.match(r"\s*(?:of\b|under\b|in\b|to\b|through\b|and\b|or\b|,|&|\[|[-\u2013\u2014]|\()", rest, re.I):
                    return None
                refs.add(match['label'].strip('() ').casefold())
                refs.update(value.strip() for value in re.findall(r"\(\s*([A-Za-z0-9]+)\s*\)", match['more'].casefold()))
            full = reference_lookups(node, instrument_id, catalog)
            same_root_mentions = [mention for mention in mentions(node.own_text)
                if mention.kind == root.kind and any(label.split('(',1)[0] == (root.label or '').casefold()
                                                     for label in mention.labels)]
            if same_root_mentions and (any(mention.unsupported for mention in same_root_mentions) or
                                       any(plan.problem for plan in full)):
                return None
            plans[branch.id].extend(full)
        references[branch.id] = refs
    if any(label not in labels for refs in references.values() for label in refs):
        return None
    references = {source:{labels[label] for label in refs} for source,refs in references.items()}
    branch_by_node = {node_id:branch for branch,group in nodes.items() for node_id in group}
    for source,links in plans.items():
        for link in links:
            if link.problem or link.target_instrument_id != instrument_id:
                continue
            destination = catalog.nodes_by_path.get((root.revision_id,link.target_reference_path))
            if destination is not None and destination.id == root.id:
                return None
            if destination is not None and destination.id in branch_by_node:
                references[source].add(branch_by_node[destination.id])
    chosen = {target.id} | qualifying
    while True:
        before = chosen.copy()
        for source, targets in references.items():
            if source in chosen or chosen & targets:
                chosen.add(source)
                chosen.update(targets)
        if chosen == before:
            break
    related = {root.id}
    qualifications = set()
    for branch_id in chosen:
        related.update(nodes[branch_id])
        if branch_id != target.id:
            qualifications.update(nodes[branch_id])
    return ReferenceScope(target.id, frozenset(related), frozenset(qualifications))
