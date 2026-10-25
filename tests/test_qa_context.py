"""Step 12 tests use synthetic laws and an in-memory publication graph."""
import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace as Row

import pytest

from app.core.qa_context_config import ContextSettings
from app.repositories.qa_context_repository import ContextCatalog, ContextScopeError, PublishedItem
from app.schemas.qa_evidence import RerankResult, RerankedCandidate
from app.services.qa_context import ContextExpander
from tests.qa_fixtures import CHUNK_SET, INSTRUMENT, PUBLICATION, REVISION, VERSION, candidate, identity, retrieval


def node(name, *, parent=None, kind="section", label="1", heading=None, text="Text.", revision=REVISION):
    return Row(id=identity(name), revision_id=revision, parent_id=parent,
               kind=kind, label=label, heading=heading, own_text=text,
               reference_path=f"f:test/{name}", ordinal=1, source_status=None)


def chunk(name, root, *, seq=1, total=1, text="Published passage.", revision=REVISION,
          chunk_set=CHUNK_SET):
    return Row(id=identity(name), revision_id=revision, chunk_set_id=chunk_set,
               root_provision_id=root.id, sequence_in_root=seq, root_total_chunks=total,
               kind=root.kind, label=root.label, heading=root.heading,
               reference_path=root.reference_path, chunk_text=text)


class FakeCatalog(ContextCatalog):
    def __init__(self, items, chunks, nodes, members, refs=None, missing=None):
        super().__init__(None, items, chunks, nodes, members, refs or defaultdict(list))
        self.missing = missing or set()

    async def chunk_sources(self, ids):
        return {chunk_id: [] if chunk_id in self.missing else [source_ref(chunk_id)]
                for chunk_id in ids}

    async def provision_sources(self, ids):
        return {provision_id: [] if provision_id in self.missing else [source_ref(provision_id)]
                for provision_id in ids}


def source_ref(value):
    return dict(source_span_id=identity("span-" + str(value)),
                file_id=identity("file"), file_sha256="a" * 64,
                page_index=2, mapping_quality="exact", block_id=identity("block"))


class FakeRepo:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []

    @asynccontextmanager
    async def snapshot(self, publication_id, *, instrument_ids=None):
        self.calls.append(publication_id)
        if publication_id != PUBLICATION:
            raise ContextScopeError("Wrong publication")
        yield self.catalog


def graph(*, split=False, qualifier=False, definition=False, reference=False,
          unresolved=False, cycle=False, second_instrument=False):
    first = candidate(1)
    root = node("provision-1", heading="Primary section", text=first.chunk_text)
    first = first.model_copy(update={"root_total_chunks": 2 if split else 1})
    first_chunk = chunk("chunk-1", root, total=2 if split else 1, text=first.chunk_text)
    chunks = {first_chunk.id: first_chunk}
    nodes = {root.id: root}
    members = {first_chunk.id: {root.id}}
    refs = defaultdict(list)
    parent = node("part", kind="part", label="I", heading="General", text="PART I General.")
    root.parent_id = parent.id
    nodes[parent.id] = parent
    if split:
        continuation = chunk("chunk-continuation", root, seq=2, total=2,
                             text="Provided that an exception applies.")
        chunks[continuation.id] = continuation
        members[continuation.id] = {root.id}
    if qualifier:
        proviso = node("proviso", kind="other", label=None, heading="Proviso",
                       parent=root.id, text="Provided that a signed permit is required.")
        nodes[proviso.id] = proviso
    if definition:
        defined = node("definitions-seven", label="7", heading="Interpretation and definitions",
                       text="Permit means written authorisation.")
        nodes[defined.id] = defined
        definition_chunk = chunk("definition-chunk", defined, text=defined.own_text)
        chunks[definition_chunk.id] = definition_chunk
        members[definition_chunk.id] = {defined.id}
    if reference or unresolved:
        target = node("target", label="4", text="An exception applies only on Sunday.")
        nodes[target.id] = target
        target_chunk = chunk("target-chunk", target, text=target.own_text)
        chunks[target_chunk.id] = target_chunk
        members[target_chunk.id] = {target.id}
        refs[root.id].append(Row(resolution_status="resolved" if reference else "unresolved",
                                 target_instrument_id=INSTRUMENT,
                                 target_reference_path=target.reference_path))
        if cycle:
            refs[target.id].append(Row(resolution_status="resolved", target_instrument_id=INSTRUMENT,
                                       target_reference_path=root.reference_path))
    items = {INSTRUMENT: PublishedItem(INSTRUMENT, VERSION, REVISION, CHUNK_SET,
                                      "a" * 64, "Synthetic Test Act", "synthetic-test",
                                      "test-edition", "TEST", "unknown")}
    if second_instrument:
        outside = identity("other-instrument")
        refs[root.id] = [Row(resolution_status="resolved", target_instrument_id=outside,
                             target_reference_path="f:test/target")]
    catalog = FakeCatalog(items, chunks, nodes, members, refs)
    result = RerankResult(retrieval=retrieval([first]), model_name="synthetic-model",
                          candidates=(RerankedCandidate(publication_id=PUBLICATION,
                              candidate=first, reranker_score=1.2, reranker_rank=1),))
    return catalog, result


def expand(catalog, reranked, *, settings=None, token_counter=None):
    return asyncio.run(ContextExpander(FakeRepo(catalog), settings or ContextSettings(),
                                      token_counter=token_counter or (lambda text: len(text.split())))
                       .expand(reranked))


def test_expands_complete_root_parent_qualification_and_definition():
    catalog, reranked = graph(split=True, qualifier=True, definition=True)
    result = expand(catalog, reranked)
    relations = {unit.relation for unit in result.units}
    assert {"retrieved", "sibling", "parent", "qualification", "definition"} <= relations
    assert any("signed permit" in unit.text for unit in result.units)
    assert any("exception applies" in unit.text for unit in result.units)
    assert any(unit.label == "7" and unit.relation == "definition" for unit in result.units)
    assert any(issue.code == "definition_heuristic" for issue in result.issues)
    assert not any(issue.critical for issue in result.issues)
    assert len({unit.evidence_id for unit in result.units}) == len(result.units)
    assert reranked.candidates[0].candidate.rrf_score == result.reranked.candidates[0].candidate.rrf_score


def test_resolved_reference_follows_pinned_publication_and_stops_cycle():
    catalog, reranked = graph(reference=True, cycle=True)
    result = expand(catalog, reranked)
    assert any(unit.relation == "reference" and unit.label == "4" for unit in result.units)
    assert any(issue.code == "reference_cycle" for issue in result.issues)
    assert len({unit.evidence_id for unit in result.units}) == len(result.units)


def test_unresolved_reference_is_reported_without_inventing_target():
    catalog, reranked = graph(unresolved=True)
    result = expand(catalog, reranked)
    assert any(issue.code == "unresolved_reference" for issue in result.issues)
    assert not any(unit.relation == "reference" for unit in result.units)


def test_missing_split_part_is_critical():
    catalog, reranked = graph(split=True)
    catalog.chunks.pop(identity("chunk-continuation"))
    catalog.chunks_by_root[(REVISION, identity("provision-1"))].pop()
    result = expand(catalog, reranked)
    assert any(issue.code == "root_fragment_missing" and issue.critical for issue in result.issues)


def test_budget_omits_whole_required_passage_and_records_it():
    catalog, reranked = graph(split=True, qualifier=True)
    result = expand(catalog, reranked, settings=ContextSettings(max_tokens=128),
                    token_counter=lambda text: 100)
    assert any(issue.code == "budget_omission" and issue.critical for issue in result.issues)
    assert all(unit.text in {row.chunk_text for row in catalog.chunks.values()} |
               {row.own_text for row in catalog.nodes.values()} for unit in result.units)
    assert result.token_count <= 128


def test_missing_exact_source_refs_is_critical():
    catalog, reranked = graph(qualifier=True)
    catalog.missing.add(identity("proviso"))
    result = expand(catalog, reranked)
    assert any(issue.code == "source_missing" and issue.critical for issue in result.issues)


def test_candidate_version_mismatch_fails_closed():
    catalog, reranked = graph()
    changed = reranked.candidates[0].candidate.model_copy(update={"revision_id": identity("other-revision")})
    reranked = reranked.model_copy(update={"candidates": (reranked.candidates[0].model_copy(
        update={"candidate": changed}),)})
    with pytest.raises(ContextScopeError, match="differs"):
        expand(catalog, reranked)


def test_resolved_reference_cannot_escape_publication():
    catalog, reranked = graph(second_instrument=True)
    result = expand(catalog, reranked)
    assert any(issue.code == "reference_target_missing" and issue.critical for issue in result.issues)
    assert not any(unit.relation == "reference" for unit in result.units)


def test_scoped_catalog_loads_a_resolved_cross_law_target_on_demand():
    from dataclasses import replace
    catalog, reranked = graph(second_instrument=True)
    other = identity("other-instrument")
    other_revision = identity("other-revision")
    other_set = identity("other-set")
    catalog.items[other] = replace(catalog.items[INSTRUMENT], instrument_id=other,
        revision_id=other_revision, chunk_set_id=other_set,
        legal_version_id=identity("other-version"), instrument_title="Another Test Act")
    target = node("cross-law-target", text="A cross-law exception must also be applied.")
    target.revision_id = other_revision
    target.reference_path = "f:test/target"
    target_chunk = chunk("cross-law-chunk", target, text=target.own_text,
                         revision=other_revision, chunk_set=other_set)
    calls = []
    async def loader(loaded, ids):
        calls.append(ids)
        assert ids == {other}
        loaded.nodes[target.id] = target
        loaded.chunks[target_chunk.id] = target_chunk
        loaded.members[target_chunk.id] = {target.id}
    catalog._loaded_instruments = {INSTRUMENT}
    catalog._loader = loader
    result = expand(catalog, reranked)
    assert calls == [{other}]
    assert any(unit.instrument_id == other and unit.relation == "reference" for unit in result.units)
    assert not any(issue.critical for issue in result.issues)


def test_bad_token_counter_is_explicit_failure():
    catalog, reranked = graph()
    with pytest.raises(Exception, match="tokenizer"):
        expand(catalog, reranked, token_counter=lambda _: -2)


@pytest.mark.parametrize("relation,root_name", [("reference", "target"),
                                               ("definition", "definitions-seven")])
def test_secondary_sections_include_their_provisos_explanations_and_parents(relation, root_name):
    catalog, reranked = graph(**{relation: True})
    root = catalog.nodes[identity(root_name)]
    parent = node("secondary-part", kind="part", heading="Special conditions",
                  text="PART II Special conditions.")
    root.parent_id = parent.id
    proviso = node("secondary-proviso", parent=root.id, kind="other", heading="Proviso",
                   text="Provided that a signed permit is required.")
    explanation = node("secondary-explanation", parent=root.id, kind="other", heading="Explanation",
                       text="Explanation: A permit must be in writing.")
    nodes = {**catalog.nodes, parent.id: parent, proviso.id: proviso, explanation.id: explanation}
    catalog = FakeCatalog(catalog.items, catalog.chunks, nodes, catalog.members, catalog.references)
    result = expand(catalog, reranked)
    assert {proviso.id, explanation.id, parent.id} <= {unit.provision_id for unit in result.units}
    assert not any(issue.critical for issue in result.issues)


def test_reference_to_nested_clause_completes_its_split_section():
    catalog, reranked = graph(reference=True)
    target = catalog.nodes[identity("target")]
    first = catalog.chunks[identity("target-chunk")]
    first.root_total_chunks = 2
    continuation = chunk("reference-continuation", target, seq=2, total=2,
                         text="The remaining conditions also apply.")
    child = node("referenced-clause", parent=target.id, kind="paragraph", label="a",
                 text=continuation.chunk_text)
    catalog.references[identity("provision-1")][0].target_reference_path = child.reference_path
    catalog = FakeCatalog(catalog.items, {**catalog.chunks, continuation.id: continuation},
                          {**catalog.nodes, child.id: child},
                          {**catalog.members, continuation.id: {child.id}}, catalog.references)
    result = expand(catalog, reranked)
    references = [unit for unit in result.units if unit.relation == "reference"]
    assert {unit.chunk_id for unit in references} == {first.id, continuation.id}
    assert not any(issue.critical for issue in result.issues)


@pytest.mark.parametrize("origin", ["definition", "qualification"])
def test_references_inside_added_definitions_and_provisos_are_followed(origin):
    catalog, reranked = graph(reference=True, definition=True, qualifier=True)
    source_id = identity("definitions-seven" if origin == "definition" else "proviso")
    catalog.references[source_id] = catalog.references.pop(identity("provision-1"))
    result = expand(catalog, reranked)
    assert any(unit.relation == "reference" and unit.provision_id == identity("target")
               for unit in result.units)


def test_incomplete_reference_section_is_critical():
    catalog, reranked = graph(reference=True)
    catalog.chunks[identity("target-chunk")].root_total_chunks = 2
    result = expand(catalog, reranked)
    assert any(issue.code == "root_fragment_missing" and issue.critical for issue in result.issues)


def test_definition_is_omitted_as_a_whole_when_its_exception_does_not_fit():
    catalog, reranked = graph(definition=True)
    definition = catalog.nodes[identity("definitions-seven")]
    proviso = node("definition-proviso", parent=definition.id, kind="other", heading="Proviso",
                   text="Provided that an exception applies.")
    catalog = FakeCatalog(catalog.items, catalog.chunks, {**catalog.nodes, proviso.id: proviso},
                          catalog.members, catalog.references)
    result = expand(catalog, reranked, settings=ContextSettings(max_tokens=128),
                    token_counter=lambda text: 100 if text == proviso.own_text else len(text.split()))
    assert all(unit.provision_id not in {definition.id, proviso.id} for unit in result.units)
    assert any(issue.code == "budget_omission" for issue in result.issues)
    assert not any(issue.critical for issue in result.issues)


def test_reference_with_missing_exception_source_blocks_the_gate():
    from app.services.qa_evidence_gate import EvidenceGate
    catalog, reranked = graph(reference=True)
    target = catalog.nodes[identity("target")]
    proviso = node("reference-proviso", parent=target.id, kind="other", heading="Proviso",
                   text="Provided that a signed permit is required.")
    catalog = FakeCatalog(catalog.items, catalog.chunks, {**catalog.nodes, proviso.id: proviso},
                          catalog.members, catalog.references, missing={proviso.id})
    result = expand(catalog, reranked)
    assert any(issue.code == "source_missing" and issue.critical for issue in result.issues)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"


def test_unresolved_reference_blocks_a_complete_answer():
    from app.services.qa_evidence_gate import EvidenceGate
    catalog, reranked = graph(unresolved=True)
    assert EvidenceGate().decide(expand(catalog, reranked)).outcome == "insufficient_evidence"


@pytest.mark.parametrize("failure", ["unresolved", "unpublished", "missing_target", "missing_source", "depth", "budget"])
def test_incomplete_optional_definition_dependencies_are_omitted_together(failure):
    from app.services.qa_evidence_gate import EvidenceGate
    catalog, reranked = graph(reference=True, definition=True)
    definition_id = identity("definitions-seven")
    catalog.references[definition_id] = catalog.references.pop(identity("provision-1"))
    ref = catalog.references[definition_id][0]
    settings = ContextSettings()
    token_counter = None
    if failure == "unresolved":
        ref.resolution_status = "unresolved"
    elif failure == "unpublished":
        ref.target_instrument_id = identity("unpublished-law")
    elif failure == "missing_target":
        ref.target_reference_path = "missing/path"
    elif failure == "missing_source":
        catalog.missing.add(identity("target-chunk"))
    elif failure == "depth":
        settings = ContextSettings(max_reference_depth=0)
    elif failure == "budget":
        settings = ContextSettings(max_tokens=128)
        token_counter = lambda text: 100 if "only on Sunday" in text else len(text.split())
    result = expand(catalog, reranked, settings=settings, token_counter=token_counter)
    baseline_catalog, baseline_reranked = graph()
    baseline = expand(baseline_catalog, baseline_reranked)
    # Neither the incomplete definition nor a partly expanded dependency leaks
    # into the prompt. Required text and all three budgets are restored exactly.
    assert result.units == baseline.units
    assert (result.token_count, result.char_count) == (baseline.token_count, baseline.char_count)
    assert any(issue.code == "optional_context_omitted" for issue in result.issues)
    assert not any(issue.critical for issue in result.issues)
    assert EvidenceGate().decide(result).outcome == "ready_for_prompt"


def test_optional_rollback_preserves_shared_required_parents_and_references():
    catalog, reranked = graph(reference=True, definition=True)
    definition_id = identity("definitions-seven")
    catalog.nodes[definition_id].parent_id = identity("part")
    catalog.references[definition_id] = [
        catalog.references[identity("provision-1")][0],
        Row(resolution_status="unresolved", target_instrument_id=None, target_reference_path=None),
    ]
    result = expand(catalog, reranked)
    assert not any(unit.provision_id == definition_id for unit in result.units)
    assert {identity("provision-1"), identity("target"), identity("part")} <= {
        unit.provision_id for unit in result.units}
    assert len({unit.evidence_id for unit in result.units}) == len(result.units)
    assert not any(issue.critical for issue in result.issues)


def test_definition_required_by_retrieved_section_cannot_be_silently_omitted():
    from app.services.qa_evidence_gate import EvidenceGate
    catalog, reranked = graph(reference=True, definition=True)
    definition = catalog.nodes[identity("definitions-seven")]
    catalog.references[identity("provision-1")][0].target_reference_path = definition.reference_path
    catalog.references[definition.id] = [Row(resolution_status="unresolved",
        target_instrument_id=None, target_reference_path=None)]
    result = expand(catalog, reranked)
    assert any(issue.code == "unresolved_reference" and issue.critical for issue in result.issues)
    assert not any(issue.code == "optional_context_omitted" for issue in result.issues)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"


def test_failed_optional_branch_does_not_poison_a_later_complete_definition():
    catalog, reranked = graph(reference=True, definition=True)
    definition = catalog.nodes[identity("definitions-seven")]
    catalog.references[definition.id] = catalog.references.pop(identity("provision-1"))
    target = catalog.nodes[identity("target")]
    # The first optional branch adds a valid reference, then meets an unresolved
    # one. The second definition must be able to include that valid target anew.
    definition.ordinal = 2
    target.ordinal = 3
    unresolved_child = node("optional-bad-reference", parent=definition.id,
                            kind="other", heading="Explanation")
    unresolved_child.ordinal = 4
    other = node("other-definition", label="8", heading="Definitions", text="A second complete definition.")
    other.ordinal = 5
    other_chunk = chunk("other-definition-chunk", other, text=other.own_text)
    catalog.references[unresolved_child.id] = [Row(resolution_status="unresolved",
        target_instrument_id=None, target_reference_path=None)]
    catalog.references[other.id] = list(catalog.references[definition.id])
    catalog = FakeCatalog(catalog.items, {**catalog.chunks, other_chunk.id: other_chunk},
        {**catalog.nodes, unresolved_child.id: unresolved_child, other.id: other},
        {**catalog.members, other_chunk.id: {other.id}}, catalog.references)
    result = expand(catalog, reranked)
    assert not any(unit.provision_id in {definition.id, unresolved_child.id} for unit in result.units)
    assert {other.id, target.id} <= {unit.provision_id for unit in result.units}
    assert any(issue.code == "optional_context_omitted" for issue in result.issues)
    assert not any(issue.critical for issue in result.issues)


def test_required_reference_gets_budget_before_optional_definition():
    catalog, reranked = graph(reference=True, definition=True)
    target_id = identity("target")
    result = expand(catalog, reranked, settings=ContextSettings(max_tokens=128),
                    token_counter=lambda text: 100 if "written authorisation" in text else len(text.split()))
    assert any(unit.provision_id == target_id for unit in result.units)
    assert not any(unit.provision_id == identity("definitions-seven") for unit in result.units)
    assert not any(issue.critical for issue in result.issues)


def test_optional_reference_cycle_can_keep_its_complete_source_group():
    catalog, reranked = graph(reference=True, definition=True)
    definition_id = identity("definitions-seven")
    catalog.references[definition_id] = catalog.references.pop(identity("provision-1"))
    catalog.references[identity("target")] = [Row(resolution_status="resolved",
        target_instrument_id=INSTRUMENT, target_reference_path=catalog.nodes[definition_id].reference_path)]
    result = expand(catalog, reranked)
    assert {definition_id, identity("target")} <= {unit.provision_id for unit in result.units}
    assert any(issue.code == "reference_cycle" for issue in result.issues)
    assert not any(issue.critical for issue in result.issues)


def test_repealed_empty_qualifier_parent_does_not_expose_its_children():
    catalog, reranked = graph(qualifier=True)
    proviso = catalog.nodes[identity("proviso")]
    proviso.own_text, proviso.source_status = "", "repealed"
    child = node("repealed-child", parent=proviso.id, kind="paragraph",
                 text="This removed condition must not be used as operative law.")
    catalog = FakeCatalog(catalog.items, catalog.chunks, {**catalog.nodes, child.id: child},
                          catalog.members, catalog.references)
    result = expand(catalog, reranked)
    assert not any(unit.provision_id == child.id for unit in result.units)
    assert any(issue.code == "qualification_missing" and issue.critical for issue in result.issues)
