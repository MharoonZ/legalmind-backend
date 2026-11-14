"""Step 12: expand published passages without changing search or source text."""
from __future__ import annotations

import re
from collections import deque
from functools import lru_cache

from app.core.qa_config import get_reranker_settings
from app.core.qa_context_config import ContextSettings, get_context_settings
from app.repositories.qa_context_repository import ContextScopeError
from app.schemas.qa_context import ContextIssue, ContextReference, ContextResult, ContextUnit
from app.schemas.qa_evidence import RerankResult
from app.services.qa.references import reference_lookups
from app.services.qa.context_scope import nested_reference_scope


class ContextTokenizationError(RuntimeError):
    """The complete context could not be sized; never truncate legal text."""


@lru_cache(maxsize=1)
def _tokenizer():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(
        get_reranker_settings().model_name, trust_remote_code=False,
        local_files_only=True,
    )


def count_context_tokens(value: str) -> int:
    try:
        return len(_tokenizer().encode(value, add_special_tokens=False, truncation=False))
    except Exception as exc:
        raise ContextTokenizationError("The context tokenizer is unavailable; cannot enforce its budget.") from exc


class ContextExpander:
    def __init__(self, repository, settings: ContextSettings | None = None, *, token_counter=None):
        self.repository = repository
        self.settings = settings or get_context_settings()
        self.token_counter = token_counter or count_context_tokens

    async def expand_packed(self, reranked: RerankResult) -> ContextResult:
        return await self.expand(reranked, pack_complete_roots=True)

    async def expand(self, reranked: RerankResult, *, pack_complete_roots=False) -> ContextResult:
        base = dict(reranked=reranked, token_limit=self.settings.max_tokens,
                    char_limit=self.settings.max_chars, unit_limit=self.settings.max_units)
        if reranked.retrieval.status != "ok" or not reranked.candidates:
            return ContextResult(**base)
        publication_id = reranked.retrieval.publication_id
        if publication_id is None:
            raise ContextScopeError("Retrieval did not pin a publication.")
        units, issues, reference_links = [], [], []
        seen, token_count, char_count = set(), 0, 0
        original = {row.candidate.chunk_id: row for row in reranked.candidates}
        packing = pack_complete_roots and not any(row.candidate.exact_rank for row in reranked.candidates)
        coverage_limited = False

        async with self.repository.snapshot(publication_id, instrument_ids={
                row.candidate.instrument_id for row in reranked.candidates}) as catalog:
            def item_for(instrument_id):
                item = catalog.items.get(instrument_id)
                if item is None:
                    raise ContextScopeError("A candidate instrument is not in the publication.")
                return item

            def check_chunk(chunk, item):
                if (chunk.chunk_set_id != item.chunk_set_id or
                    chunk.revision_id != item.revision_id):
                    raise ContextScopeError("Context chunk is outside the pinned source version.")

            def ancestors(node):
                trail, visited = [], set()
                parent_id = node.parent_id
                while parent_id is not None:
                    if parent_id in visited:
                        raise ContextScopeError("Cycle in a published provision hierarchy.")
                    visited.add(parent_id)
                    parent = catalog.nodes.get(parent_id)
                    if parent is None or parent.revision_id != node.revision_id:
                        raise ContextScopeError("Parent is missing from the published revision.")
                    trail.append(parent)
                    parent_id = parent.parent_id
                return trail

            def ancestor_labels(node):
                return tuple(f"{p.kind} {p.label or ''}: {p.heading or ''}".strip()
                             for p in reversed(ancestors(node)))

            def add_issue(code, detail, *, critical=False, source_id=None):
                issues.append(ContextIssue(code=code, detail=detail, critical=critical,
                                           source_id=source_id))

            def add_unit(unit, *, required=False):
                nonlocal token_count, char_count
                if unit.evidence_id in seen:
                    return True
                try:
                    cost = self.token_counter(unit.text) + 16  # Reserve ID/metadata overhead.
                except ContextTokenizationError:
                    raise
                except Exception as exc:
                    raise ContextTokenizationError("Could not count complete context tokens.") from exc
                if not isinstance(cost, int) or cost < 16:
                    raise ContextTokenizationError("The context tokenizer returned an invalid count.")
                if (len(units) + 1 > self.settings.max_units or
                    char_count + len(unit.text) > self.settings.max_chars or
                    token_count + cost > self.settings.max_tokens):
                    add_issue("budget_omission", "A whole passage was omitted by the configured context budget.",
                              critical=required, source_id=unit.evidence_id)
                    return False
                if not unit.source_refs or any(ref.get("mapping_quality") != "exact" for ref in unit.source_refs):
                    add_issue("source_missing", "A passage lacks exact PDF source spans.",
                              critical=required, source_id=unit.evidence_id)
                    return False
                units.append(unit)
                seen.add(unit.evidence_id)
                char_count += len(unit.text)
                token_count += cost
                return True

            def add_group(group, *, required):
                # A definition/reference must not leave its rule in the prompt
                # after its exception was dropped to satisfy the budget.
                nonlocal token_count, char_count
                count, old_tokens, old_chars = len(units), token_count, char_count
                old_seen = seen.copy()
                for unit in group:
                    if not add_unit(unit, required=required):
                        del units[count:]
                        seen.intersection_update(old_seen)
                        token_count, char_count = old_tokens, old_chars
                        return False
                return True

            def group_fits(passages, *, required):
                # Check complete-group budgets before fetching its source spans.
                # Discarded lower-ranked groups otherwise incurred remote reads
                # even though their text could never enter the prompt.
                pending = {key: value for key, value in passages if key not in seen}
                costs = [self.token_counter(value) + 16 for value in pending.values()]
                if any(isinstance(cost, bool) or not isinstance(cost, int) or cost < 16 for cost in costs):
                    raise ContextTokenizationError("The context tokenizer returned an invalid count.")
                if (len(units) + len(pending) > self.settings.max_units or
                        char_count + sum(map(len, pending.values())) > self.settings.max_chars or
                        token_count + sum(costs) > self.settings.max_tokens):
                    add_issue("budget_omission", "A complete source group did not fit the context budget.",
                              critical=required, source_id=next(iter(pending), None))
                    return False
                return True

            async def chunk_unit(chunk, instrument_id, relation):
                item = item_for(instrument_id)
                check_chunk(chunk, item)
                match = original.get(chunk.id)
                if match:
                    candidate = match.candidate
                    if (candidate.instrument_id != instrument_id or
                        candidate.legal_version_id != item.legal_version_id or
                        candidate.revision_id != item.revision_id or
                        candidate.chunk_set_id != item.chunk_set_id or
                        candidate.chunk_text != chunk.chunk_text):
                        raise ContextScopeError("Candidate differs from the published chunk.")
                    refs = candidate.source_refs
                else:
                    refs = (await catalog.chunk_sources([chunk.id])).get(chunk.id, [])
                root = catalog.nodes.get(chunk.root_provision_id)
                if root is None or root.revision_id != item.revision_id:
                    raise ContextScopeError("Chunk root is missing from the pinned revision.")
                return ContextUnit(
                    evidence_id=f"chunk:{chunk.id}", source_kind="chunk", relation=relation,
                    publication_id=publication_id, instrument_id=instrument_id,
                    legal_version_id=item.legal_version_id, revision_id=item.revision_id,
                    chunk_set_id=item.chunk_set_id, chunk_id=chunk.id,
                    provision_id=chunk.root_provision_id, instrument_title=item.instrument_title,
                    edition_key=item.edition_key, jurisdiction_code=item.jurisdiction_code,
                    applicability_status=item.applicability_status, kind=chunk.kind,
                    label=chunk.label, heading=chunk.heading, reference_path=chunk.reference_path,
                    text=chunk.chunk_text, source_refs=refs,
                    retrieval_rank=match.candidate.rank if match else None,
                    reranker_rank=match.reranker_rank if match else None,
                    reranker_score=match.reranker_score if match else None,
                    ancestor_labels=ancestor_labels(root),
                )

            async def provision_unit(node, instrument_id, relation):
                item = item_for(instrument_id)
                if node.revision_id != item.revision_id:
                    raise ContextScopeError("Provision is outside the pinned revision.")
                refs = (await catalog.provision_sources([node.id])).get(node.id, [])
                return ContextUnit(
                    evidence_id=f"provision:{node.id}", source_kind="provision", relation=relation,
                    publication_id=publication_id, instrument_id=instrument_id,
                    legal_version_id=item.legal_version_id, revision_id=item.revision_id,
                    chunk_set_id=item.chunk_set_id, provision_id=node.id,
                    instrument_title=item.instrument_title, edition_key=item.edition_key,
                    jurisdiction_code=item.jurisdiction_code,
                    applicability_status=item.applicability_status, kind=node.kind,
                    label=node.label, heading=node.heading, reference_path=node.reference_path,
                    text=node.own_text, source_refs=refs, ancestor_labels=ancestor_labels(node),
                )

            selected_roots = []
            selected_root_ids = set()
            for selected in reranked.candidates:
                candidate = selected.candidate
                chunk = catalog.chunks.get(candidate.chunk_id)
                if chunk is None:
                    raise ContextScopeError("A retrieved chunk is absent from the active publication.")
                if not packing:
                    add_unit(await chunk_unit(chunk, candidate.instrument_id, "retrieved"), required=True)
                key = (candidate.instrument_id, chunk.root_provision_id)
                if key not in selected_root_ids:
                    selected_root_ids.add(key)
                    selected_roots.append(key)

            expanded_roots = {}

            def root_parts(instrument_id, root_id, *, required):
                item = item_for(instrument_id)
                parts = catalog.chunks_by_root.get((item.revision_id, root_id), [])
                if (not parts or
                    [p.sequence_in_root for p in parts] != list(range(1, parts[0].root_total_chunks + 1)) or
                    any(p.root_total_chunks != len(parts) for p in parts)):
                    add_issue("root_fragment_missing", "A selected provision has missing published parts.",
                              critical=required, source_id=f"provision:{root_id}")
                    return None
                for part in parts:
                    check_chunk(part, item)
                return parts

            async def complete_root(instrument_id, root_id, relation, *, required):
                key = (instrument_id, root_id)
                if key in expanded_roots:
                    return expanded_roots[key]
                parts = root_parts(instrument_id, root_id, required=required)
                if parts is None:
                    return None
                related = {root_id}
                for chunk in parts:
                    related.update(catalog.members.get(chunk.id, ()))

                # Apply the same source-backed closure to retrieved sections,
                # definitions, and reference targets. Generic notes stay excluded.
                root = catalog.nodes[root_id]
                qualifications = []
                for node in sorted(catalog.nodes.values(), key=lambda row: row.ordinal):
                    if node.revision_id != root.revision_id or node.id == root_id:
                        continue
                    trail = ancestors(node)
                    if root_id not in {p.id for p in trail}:
                        continue
                    qualifier = node.heading in {"Proviso", "Explanation"} or any(
                        p.heading in {"Proviso", "Explanation"} for p in trail)
                    if qualifier and node.own_text.strip():
                        if any(p.source_status in {"repealed", "omitted", "deleted"}
                               for p in (node, *trail)):
                            add_issue("qualification_missing", "A qualification is marked non-operative in the source.",
                                      critical=required, source_id=f"provision:{node.id}")
                            return None
                        qualifications.append(node)
                        related.add(node.id)
                parents = [parent for parent in ancestors(root) if parent.own_text.strip()]
                related.update(parent.id for parent in parents)
                if not group_fits(
                        [(f"chunk:{part.id}", part.chunk_text) for part in parts] +
                        [(f"provision:{node.id}", node.own_text) for node in (*qualifications, *parents)],
                        required=required):
                    return None
                # Fetch exact source spans once per whole group, rather than one
                # network round trip for every parent, qualifier or split chunk.
                await catalog.chunk_sources([part.id for part in parts if part.id not in original])
                await catalog.provision_sources([node.id for node in (*qualifications, *parents)])
                group = [await chunk_unit(part, instrument_id,
                    "retrieved" if packing and part.id in original else relation) for part in parts]
                group.extend([await provision_unit(node, instrument_id, "qualification")
                              for node in qualifications])
                group.extend([await provision_unit(parent, instrument_id, "parent")
                              for parent in parents])
                if not add_group(group, required=required):
                    return None
                expanded_roots[key] = related
                return related

            async def complete_scoped(instrument_id, root_id, scope, *, required):
                key = (instrument_id, scope.target_id)
                if key in expanded_roots:
                    return expanded_roots[key]
                if root_parts(instrument_id, root_id, required=required) is None:
                    return None
                target = catalog.nodes[scope.target_id]
                parents = ancestors(target)
                related = set(scope.related) | {parent.id for parent in parents}
                parent_ids = {parent.id for parent in parents}
                nodes = sorted((catalog.nodes[value] for value in related), key=lambda n:(n.ordinal, str(n.id)))
                if any(node.source_status in {"repealed", "omitted", "deleted"} for node in nodes):
                    add_issue("qualification_missing", "A scoped reference contains a non-operative provision or qualification.",
                              critical=required, source_id=f"provision:{scope.target_id}")
                    return None
                nodes = [node for node in nodes if node.own_text.strip()]
                if not group_fits([(f"provision:{node.id}", node.own_text) for node in nodes], required=required):
                    return None
                await catalog.provision_sources([node.id for node in nodes])
                group = [await provision_unit(node, instrument_id,
                    "parent" if node.id in parent_ids else
                    "qualification" if node.id in scope.qualifications else "reference") for node in nodes]
                if not add_group(group, required=required):
                    return None
                expanded_roots[key] = related
                return related

            required_seeds = []
            for instrument_id, root_id in selected_roots:
                if not packing and await complete_root(instrument_id, root_id, "sibling", required=True) is not None:
                    required_seeds.append((instrument_id, root_id, 0))

            # Reviewed links and unique same-revision numbered source mentions
            # may be followed. Cross-law guesses and DB mutations are forbidden.
            closed_roots = set()

            async def follow_references(seeds, *, required):
                queue = deque(seeds)
                scheduled = {(instrument_id, root_id) for instrument_id, root_id, _ in seeds}
                visited, complete = set(), True
                while queue:
                    instrument_id, provision_id, depth = queue.popleft()
                    key = (instrument_id, provision_id)
                    if key in visited:
                        continue
                    visited.add(key)
                    plans = {node: reference_lookups(catalog.nodes[node], instrument_id, catalog)
                             for node in sorted(expanded_roots[key], key=lambda value: catalog.nodes[value].ordinal)}
                    # Validate the stored text used for derived lookups against
                    # exact extraction spans once per group, inside this snapshot.
                    derived = {node for node, refs in plans.items()
                               if any(ref.method == "same_revision_lookup" and not ref.problem for ref in refs)}
                    source_refs = await catalog.provision_sources(derived) if derived else {}
                    for node, refs in plans.items():
                        for ref in refs:
                            if ref.problem:
                                add_issue("unresolved_reference", ref.problem,
                                          critical=required, source_id=f"provision:{node}")
                                complete = False
                                continue
                            if ref.method == "same_revision_lookup" and (
                                    not source_refs.get(node) or any(
                                        source.get("mapping_quality") != "exact" for source in source_refs[node])):
                                add_issue("source_missing", "A reference lookup lacks exact stored source spans.",
                                          critical=required, source_id=f"provision:{node}")
                                complete = False
                                continue
                            target_item = catalog.items.get(ref.target_instrument_id)
                            if target_item is None:
                                add_issue("reference_target_missing", "A resolved reference targets an unpublished law.",
                                          critical=required, source_id=f"provision:{node}")
                                complete = False
                                continue
                            await catalog.ensure_instruments({target_item.instrument_id})
                            target = catalog.nodes_by_path.get((target_item.revision_id, ref.target_reference_path))
                            if target is None or any(
                                    n.source_status in {"repealed", "omitted", "deleted"}
                                    for n in (target, *ancestors(target))):
                                add_issue("reference_target_missing", "A resolved reference has no target in the pinned edition.",
                                          critical=required, source_id=f"provision:{node}")
                                complete = False
                                continue
                            target_chunks = (catalog.chunks_by_root.get((target_item.revision_id, target.id)) or
                                             catalog.chunks_by_provision.get(target.id, ()))
                            if not target_chunks:
                                add_issue("reference_target_missing", "A resolved target has no published operative chunk.",
                                          critical=required, source_id=f"provision:{node}")
                                complete = False
                                continue
                            link = ContextReference(
                                source_provision_id=node, target_instrument_id=target_item.instrument_id,
                                target_provision_id=target.id, target_reference_path=target.reference_path,
                                raw_reference=ref.raw_reference, method=ref.method,
                                source_char_start=ref.source_start, source_char_end=ref.source_end)
                            for target_root in dict.fromkeys(chunk.root_provision_id for chunk in target_chunks):
                                full_key = (target_item.instrument_id, target_root)
                                # Keep heading-discovered optional definitions on
                                # their existing whole-group policy. Precision is
                                # for required, explicitly linked dependencies.
                                scope = (nested_reference_scope(target, catalog.nodes[target_root], catalog)
                                         if required and full_key not in scheduled and full_key not in closed_roots else None)
                                target_key = (target_item.instrument_id, scope.target_id if scope else target_root)
                                coverage_scope = scope
                                if (required and coverage_scope is None and target.kind in {"subsection", "paragraph"}
                                        and full_key not in scheduled and full_key not in closed_roots):
                                    branch = next((n for n in (target,*ancestors(target)) if n.parent_id == target_root),None)
                                    if branch is not None:
                                        coverage_scope = nested_reference_scope(branch,catalog.nodes[target_root],catalog)
                                covered = required and coverage_scope is not None and any(
                                    group[0] == target_item.instrument_id and coverage_scope.related <= expanded_roots[group]
                                    for group in scheduled | closed_roots)
                                if covered or target_key in scheduled or target_key in closed_roots:
                                    if link not in reference_links:
                                        reference_links.append(link)
                                    add_issue("reference_cycle", "A complete or already scheduled legal reference was reused.",
                                              source_id=f"provision:{node}")
                                    continue
                                if depth >= self.settings.max_reference_depth:
                                    add_issue("reference_depth_limit", "Reference expansion reached its configured depth.",
                                              critical=required, source_id=f"provision:{node}")
                                    complete = False
                                    continue
                                added = (await complete_scoped(target_item.instrument_id, target_root, scope, required=required)
                                         if scope else await complete_root(target_item.instrument_id, target_root, "reference", required=required))
                                if added is None:
                                    complete = False
                                    continue
                                if link not in reference_links:
                                    reference_links.append(link)
                                scheduled.add(target_key)
                                queue.append((target_item.instrument_id, target_key[1], depth + 1))
                if complete:
                    closed_roots.update(visited)
                return complete

            # Required sections and their dependencies have first claim on the
            # budget. A heading-discovered definition must never make a required
            # reference optional, even when both point to the same section.
            if packing:
                # Commit a ranked root only together with all its qualifications
                # and required references. Failed groups cannot contaminate a
                # retained rule or force the whole question down to one root.
                for instrument_id, root_id in selected_roots:
                    count, old_tokens, old_chars = len(units), token_count, char_count
                    old_issues, old_links = len(issues), len(reference_links)
                    old_seen, old_roots, old_closed = seen.copy(), expanded_roots.copy(), closed_roots.copy()
                    added = await complete_root(instrument_id, root_id, "sibling", required=True)
                    complete = added is not None and await follow_references(
                        [(instrument_id, root_id, 0)], required=True)
                    if not complete or any(issue.critical for issue in issues[old_issues:]):
                        del units[count:]
                        del reference_links[old_links:]
                        del issues[old_issues:]
                        seen.clear()
                        seen.update(old_seen)
                        expanded_roots.clear()
                        expanded_roots.update(old_roots)
                        closed_roots.clear()
                        closed_roots.update(old_closed)
                        token_count, char_count = old_tokens, old_chars
                        coverage_limited = True
                        add_issue("optional_context_omitted",
                            "A ranked source group and its required dependencies could not be retained completely.",
                            source_id=f"provision:{root_id}")
            else:
                await follow_references(required_seeds, required=True)
            definition_instruments = set()
            numbered_lookup = any(row.candidate.matched_reference_paths for row in reranked.candidates)
            asks_definition = re.search(r"\b(?:defin\w*|mean(?:s|ing)?|interpret\w*)\b",
                                        reranked.retrieval.query, re.I)
            # A numbered lookup already has its required references. Only add
            # heading-based definitions if the question calls for them.
            instruments = deque(dict.fromkeys(instrument_id for instrument_id, _ in expanded_roots)
                                if not numbered_lookup or asks_definition else ())
            while instruments and not any(issue.critical for issue in issues):
                instrument_id = instruments.popleft()
                if instrument_id in definition_instruments:
                    continue
                definition_instruments.add(instrument_id)
                item = item_for(instrument_id)
                definitions = [n for n in catalog.nodes.values()
                               if n.revision_id == item.revision_id and
                               n.kind in {"section", "rule", "article"} and
                               re.search(r"\b(definitions?|interpretation)\b", n.heading or "", re.I)]
                definitions.sort(key=lambda row: row.ordinal)
                for node in definitions[:self.settings.max_definition_sections]:
                    if (instrument_id, node.id) in closed_roots:
                        continue
                    add_issue("definition_heuristic", "Definition section selected by heading; term scope is not verified.",
                              source_id=f"provision:{node.id}")
                    count, old_tokens, old_chars = len(units), token_count, char_count
                    old_links = len(reference_links)
                    old_seen, old_roots, old_closed = seen.copy(), expanded_roots.copy(), closed_roots.copy()
                    added = await complete_root(instrument_id, node.id, "definition", required=False)
                    complete = added is not None and await follow_references(
                        [(instrument_id, node.id, 0)], required=False)
                    if not complete:
                        # Roll back the entire optional dependency group. Keeping
                        # its main rule but dropping a referenced exception would
                        # give the model incomplete law. Shared required units stay.
                        del units[count:]
                        del reference_links[old_links:]
                        seen.clear()
                        seen.update(old_seen)
                        expanded_roots.clear()
                        expanded_roots.update(old_roots)
                        closed_roots.clear()
                        closed_roots.update(old_closed)
                        token_count, char_count = old_tokens, old_chars
                        add_issue("optional_context_omitted", "An optional definition and its dependencies were omitted because their complete source context was unavailable.",
                                  source_id=f"provision:{node.id}")
                    else:
                        instruments.extend(dict.fromkeys(
                            instrument for instrument, _ in expanded_roots
                            if instrument not in definition_instruments))
        if packing:
            retained = {unit.chunk_id for unit in units if unit.relation == "retrieved"}
            base["reranked"] = reranked.model_copy(update={
                "candidates": tuple(row for row in reranked.candidates if row.candidate.chunk_id in retained)})
        return ContextResult(**base, units=tuple(units), issues=tuple(issues), coverage_limited=coverage_limited,
                             reference_links=tuple(reference_links),
                             token_count=token_count, char_count=char_count)
