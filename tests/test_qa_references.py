"""Structural reference expansion without a database, PDF files, or AI calls."""
from types import SimpleNamespace as Row

import pytest

from app.core.qa_context_config import ContextSettings
from tests.qa_fixtures import prepared_context
from app.services.qa.references import mentions
from tests.qa_fixtures import INSTRUMENT, identity
from tests.test_qa_context import FakeCatalog, chunk, expand, graph, node


def reference_graph(text, *, targets=("4",), raw="section 4", status="unresolved"):
    catalog, reranked = graph()
    root = catalog.nodes[identity("provision-1")]
    root.own_text = text
    catalog.chunks[identity("chunk-1")].chunk_text = text
    selected = reranked.candidates[0]
    selected = selected.model_copy(update={"candidate": selected.candidate.model_copy(update={"chunk_text": text})})
    reranked = reranked.model_copy(update={"candidates": (selected,)})
    for label in targets:
        target = node("lookup-" + label, label=label, text=f"The complete condition in section {label} applies.")
        target.ordinal = 10 + len(catalog.nodes)
        part = chunk("lookup-chunk-" + label, target, text=target.own_text)
        catalog.nodes[target.id] = target
        catalog.chunks[part.id] = part
        catalog.members[part.id] = {target.id}
    # Avoid inventing a self-reference in our target fixture's sentence.
    for label in targets:
        target = catalog.nodes[identity("lookup-" + label)]
        target.own_text = f"A complete published condition numbered {label} applies."
        catalog.chunks[identity("lookup-chunk-" + label)].chunk_text = target.own_text
    if raw is not None:
        catalog.references[root.id] = [Row(raw_reference=raw, resolution_status=status,
                                          target_instrument_id=None, target_reference_path=None)]
    catalog._index_graph()
    return catalog, reranked


@pytest.mark.parametrize("text", [
    "Apply section 4 of this Act.",
    "Apply section 4 of the Act.",
    "The exception in section 4 applies.",
    "Apply SECTION 4\nof this Act.",
])
def test_unique_same_revision_reference_expands_without_changing_the_database_row(text):
    catalog, reranked = reference_graph(text)
    before = vars(catalog.references[identity("provision-1")][0]).copy()
    result = expand(catalog, reranked)
    assert any(u.provision_id == identity("lookup-4") and u.relation == "reference" for u in result.units)
    assert not any(i.critical for i in result.issues)
    link = result.reference_links[0]
    assert link.method == "same_revision_lookup" and link.target_instrument_id == INSTRUMENT
    assert text[link.source_char_start:link.source_char_end] == link.raw_reference
    assert vars(catalog.references[identity("provision-1")][0]) == before


@pytest.mark.parametrize("tail", ["4, 5, 6, 7 or 8", "4 and 5", "4, 5 and 6", "4(1) or 4(2)"])
def test_complete_reference_lists_are_parsed(tail):
    groups = mentions(f"The exceptions in sections {tail} of this Act apply.")
    assert len(groups) == 1
    assert groups[0].labels == tuple(tail.replace(",", " ").replace("and", " ").replace("or", " ").split())
    assert not groups[0].unsupported


def test_first_member_legacy_record_does_not_hide_remaining_list_members():
    catalog, reranked = reference_graph("Apply sections 4, 5 and 6 of this Act.",
                                        targets=("4", "5", "6"), raw="sections 4")
    result = expand(catalog, reranked)
    assert {link.target_provision_id for link in result.reference_links} == {
        identity("lookup-" + label) for label in ("4", "5", "6")}
    assert not any(i.critical for i in result.issues)


def test_section_four_and_section_forty_are_not_duplicate_mentions():
    catalog, reranked = reference_graph("Apply section 4 of this Act and section 40 of this Act.", targets=("4", "40"))
    result = expand(catalog, reranked)
    assert {link.target_provision_id for link in result.reference_links} == {
        identity("lookup-4"), identity("lookup-40")}
    assert not any(i.critical for i in result.issues)


def test_definition_of_act_disambiguates_the_act_from_a_different_sibling_definition():
    catalog, reranked = reference_graph("Definitions. Apply section 4 of the Act.")
    root = catalog.nodes[identity("provision-1")]
    root.heading = "Definitions."
    own = node("own-act-definition", parent=root.id, kind="paragraph", label="a",
               text='"Act" means the Synthetic Test Act;')
    other = node("other-act-definition", parent=root.id, kind="paragraph", label="b",
                 text='"Goods" means goods under the Foreign Act;')
    catalog.nodes.update({own.id: own, other.id: other})
    catalog.members[identity("chunk-1")].update({own.id, other.id})
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert any(link.target_provision_id == identity("lookup-4") for link in result.reference_links)
    assert not any(i.critical for i in result.issues)


def test_conduct_in_a_sibling_clause_is_not_misread_as_another_statute():
    catalog, reranked = reference_graph("Apply section 4 of the Act.")
    root = catalog.nodes[identity("provision-1")]
    child = node("conduct-clause", parent=root.id, kind="paragraph",
                 text="Any act on the part of a person in contravention of this Act is prohibited.")
    catalog.nodes[child.id] = child
    catalog.members[identity("chunk-1")].add(child.id)
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert not any(i.critical for i in result.issues)
    assert any(link.target_provision_id == identity("lookup-4") for link in result.reference_links)


def test_missing_middle_member_of_a_list_blocks_answer_generation():
    catalog, reranked = reference_graph("Apply sections 4, 5 and 6 of this Act.",
                                        targets=("4", "6"), raw="sections 4")
    result = expand(catalog, reranked)
    assert any(i.code == "unresolved_reference" and i.critical for i in result.issues)
    assert prepared_context(result).outcome == "insufficient_evidence"


def test_numbered_dependency_is_detected_even_if_no_ingestion_reference_row_was_saved():
    catalog, reranked = reference_graph("Apply sections 4 and 5 of this Act.", targets=("4", "5"), raw=None)
    result = expand(catalog, reranked)
    assert len(result.reference_links) == 2
    assert not any(i.critical for i in result.issues)


@pytest.mark.parametrize("text", [
    "Apply section 4 of the Criminal Procedure Code.",
    "The Foreign Act, section 4, applies.",
    "Apply section 4, Chapter XVII, of the Code of Criminal Procedure.",
    "The Foreign Act applies under section 4.",
    "The Pakistan Penal Code11, section 4, applies.",
    "Under the 9Code of Criminal Procedure, section 4 applies.",
    "Another law applies under section 4.",
    "Apply section 4 of that Act.",
    "Apply section 4 of the same Code.",
    "Apply section 4 of these Rules.",
    "Apply section 4 of this Act and the Foreign Act.",
    "Apply section 4 of this Act and the Foreign\nAct.",
    "Under the Foreign Act, apply section 4 of the Act.",
    "Apply section 4 thereof.",
])
def test_unreviewed_external_or_ambiguous_law_is_not_bound_to_a_same_number_hit(text):
    catalog, reranked = reference_graph(text)
    result = expand(catalog, reranked)
    assert any(i.code == "unresolved_reference" and i.critical for i in result.issues)
    assert not any(link.target_provision_id == identity("lookup-4") for link in result.reference_links)


def test_explicit_internal_reference_remains_separate_from_external_reference_in_same_text():
    catalog, reranked = reference_graph(
        "Apply section 4 of this Act, and section 5 of the Foreign Act.", targets=("4", "5"))
    result = expand(catalog, reranked)
    assert {link.target_provision_id for link in result.reference_links} == {identity("lookup-4")}
    assert prepared_context(result).outcome == "insufficient_evidence"


@pytest.mark.parametrize("status", ["ambiguous", "rejected"])
def test_existing_review_decision_is_not_overridden_by_automatic_lookup(status):
    catalog, reranked = reference_graph("Apply section 4 of this Act.", status=status)
    result = expand(catalog, reranked)
    assert not result.reference_links
    assert prepared_context(result).outcome == "insufficient_evidence"


@pytest.mark.parametrize("tail", ["3[59]", "4 to 6", "4 through 6", "4-6", "4\u20136", "4[59]"])
def test_amendment_markers_and_ranges_are_not_silently_reduced_to_the_first_number(tail):
    first = "3" if tail.startswith("3") else "4"
    catalog, reranked = reference_graph(f"Apply section {tail} of this Act.", targets=(first,), raw="section " + first)
    result = expand(catalog, reranked)
    assert not result.reference_links
    assert prepared_context(result).outcome == "insufficient_evidence"


def test_nested_subsection_uses_exact_target_and_preserves_selector_in_source_audit():
    text = "The officer as provided in sub-section (1) of section 4 of the Act."
    catalog, reranked = reference_graph(text)
    target = catalog.nodes[identity("lookup-4")]
    child = node("lookup-subsection", parent=target.id, kind="subsection", label="1", text="A condition.")
    catalog.nodes[child.id] = child
    catalog.members[identity("lookup-chunk-4")].add(child.id)
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert result.reference_links[0].target_provision_id == child.id
    link = result.reference_links[0]
    assert text[link.source_char_start:link.source_char_end] == "sub-section (1) of section 4"


def test_missing_explicit_subsection_is_not_replaced_by_its_parent_section():
    catalog, reranked = reference_graph("Apply section 4(2) of this Act.", raw="section 4(2)")
    assert prepared_context(expand(catalog, reranked)).outcome == "insufficient_evidence"


@pytest.mark.parametrize("reference", [
    "sub-sections (1) and (2) of section 4",
    "paragraph (a) of section 4",
    "paragraph (a) of sub-section (1) of section 4",
    "sub-section (1) of the said section 4",
    "section 4(1), (2) and (3)",
    "sections 4 & 5",
])
def test_unsupported_compound_selectors_do_not_silently_become_the_parent_section(reference):
    catalog, reranked = reference_graph(f"Apply {reference} of this Act.", raw=None)
    result = expand(catalog, reranked)
    assert not result.reference_links
    assert prepared_context(result).outcome == "insufficient_evidence"


def test_stored_reviewed_external_target_is_preserved_even_with_same_number_in_current_act():
    from dataclasses import replace
    catalog, reranked = reference_graph("Apply section 4 of the Foreign Act.", status="resolved")
    other, revision, chunk_set = identity("other-law"), identity("other-revision"), identity("other-set")
    catalog.items[other] = replace(catalog.items[INSTRUMENT], instrument_id=other, revision_id=revision,
                                   chunk_set_id=chunk_set, legal_version_id=identity("other-version"))
    target = node("reviewed-external-four", revision=revision, label="4", text="The reviewed external condition applies.")
    part = chunk("reviewed-external-chunk", target, revision=revision, chunk_set=chunk_set, text=target.own_text)
    catalog.nodes[target.id] = target
    catalog.chunks[part.id] = part
    catalog.members[part.id] = {target.id}
    row = catalog.references[identity("provision-1")][0]
    row.target_instrument_id, row.target_reference_path = other, target.reference_path
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert len(result.reference_links) == 1 and result.reference_links[0].method == "stored"
    assert result.reference_links[0].target_instrument_id == other
    assert not any(i.critical for i in result.issues)


@pytest.mark.parametrize("fault", ["duplicate", "wrong_revision", "non_operative"])
def test_target_requires_unique_operative_identity_in_the_pinned_revision(fault):
    catalog, reranked = reference_graph("Apply section 4 of this Act.")
    target = catalog.nodes[identity("lookup-4")]
    if fault == "duplicate":
        extra = node("duplicate-four", label="4")
        catalog.nodes[extra.id] = extra
    elif fault == "wrong_revision":
        target.revision_id = identity("unpublished-revision")
    else:
        target.source_status = "repealed"
    catalog._index_graph()
    assert prepared_context(expand(catalog, reranked)).outcome == "insufficient_evidence"


def test_exact_label_does_not_match_inserted_section_with_similar_number():
    catalog, reranked = reference_graph("Apply section 4 of this Act.", targets=("4A",))
    assert prepared_context(expand(catalog, reranked)).outcome == "insufficient_evidence"


def test_ancestor_external_law_context_prevents_unqualified_same_number_guess():
    catalog, reranked = reference_graph("Apply section 4.")
    catalog.nodes[identity("part")].own_text = "The Foreign Act applies to the following."
    assert prepared_context(expand(catalog, reranked)).outcome == "insufficient_evidence"


def test_external_law_in_a_sibling_clause_prevents_implicit_current_law_lookup():
    catalog, reranked = reference_graph("The main operative text applies.", raw=None)
    root = catalog.nodes[identity("provision-1")]
    first = node("scope-first", parent=root.id, kind="paragraph", label="a", text="The Foreign Act governs the following.")
    second = node("scope-second", parent=root.id, kind="paragraph", label="b", text="Apply section 4 of the Act.")
    catalog.nodes.update({first.id: first, second.id: second})
    catalog.members[identity("chunk-1")].update({first.id, second.id})
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert any(i.code == "unresolved_reference" and i.critical for i in result.issues)
    assert not any(link.source_provision_id == second.id for link in result.reference_links)


def test_derived_lookup_requires_exact_provenance_for_the_reference_source():
    catalog, reranked = reference_graph("Apply section 4 of this Act.")
    catalog.missing.add(identity("provision-1"))
    result = expand(catalog, reranked)
    assert any(i.code == "source_missing" and i.critical for i in result.issues)
    assert not result.reference_links


def test_derived_target_retains_its_split_passages_conditions_and_parents():
    catalog, reranked = reference_graph("Apply section 4 of this Act.")
    root = catalog.nodes[identity("lookup-4")]
    first = catalog.chunks[identity("lookup-chunk-4")]
    first.root_total_chunks = 2
    continuation = chunk("lookup-second", root, seq=2, total=2, text="A second required condition applies.")
    qualifier = node("lookup-proviso", parent=root.id, kind="other", label=None,
                     heading="Proviso", text="Provided that a written permit is required.")
    catalog = FakeCatalog(catalog.items, {**catalog.chunks, continuation.id: continuation},
                          {**catalog.nodes, qualifier.id: qualifier},
                          {**catalog.members, continuation.id: {root.id}}, catalog.references)
    result = expand(catalog, reranked)
    assert {identity("lookup-chunk-4"), continuation.id} <= {u.chunk_id for u in result.units}
    assert any(u.provision_id == qualifier.id for u in result.units)
    assert not any(i.critical for i in result.issues)


@pytest.mark.parametrize("failure", ["budget", "depth", "missing_source"])
def test_derived_dependencies_keep_existing_budget_depth_and_source_guards(failure):
    catalog, reranked = reference_graph("Apply section 4 of this Act.")
    settings = ContextSettings()
    counter = None
    if failure == "depth":
        settings = ContextSettings(max_reference_depth=0)
    elif failure == "budget":
        settings = ContextSettings(max_tokens=128)
        counter = lambda text: 110 if "numbered 4" in text else 10
    else:
        catalog.missing.add(identity("lookup-chunk-4"))
    result = expand(catalog, reranked, settings=settings, token_counter=counter)
    assert prepared_context(result).outcome == "insufficient_evidence"


def test_failed_optional_lookup_group_rolls_back_units_and_reference_audit_together():
    catalog, reranked = graph(definition=True)
    defined = catalog.nodes[identity("definitions-seven")]
    defined.own_text = "Apply sections 4 and 5 of this Act."
    catalog.chunks[identity("definition-chunk")].chunk_text = defined.own_text
    target = node("optional-lookup", label="4", text="The required condition applies.")
    part = chunk("optional-lookup-chunk", target, text=target.own_text)
    catalog = FakeCatalog(catalog.items, {**catalog.chunks, part.id: part},
                          {**catalog.nodes, target.id: target},
                          {**catalog.members, part.id: {target.id}}, catalog.references)
    result = expand(catalog, reranked)
    assert not any(u.provision_id in {defined.id, target.id} for u in result.units)
    assert not any(link.source_provision_id in {defined.id, target.id} for link in result.reference_links)
    assert any(i.code == "optional_context_omitted" for i in result.issues)
    assert not any(i.critical for i in result.issues)


def test_conflicting_reviewed_target_and_explicit_same_law_text_are_not_combined():
    catalog, reranked = reference_graph("Apply section 4 of this Act.", targets=("4", "5"), status="resolved")
    row = catalog.references[identity("provision-1")][0]
    row.target_instrument_id = INSTRUMENT
    row.target_reference_path = catalog.nodes[identity("lookup-5")].reference_path
    assert prepared_context(expand(catalog, reranked)).outcome == "insufficient_evidence"
