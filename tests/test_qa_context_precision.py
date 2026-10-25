"""Precise nested references retain complete scoped conditions and source guards."""
from types import SimpleNamespace as Row

import pytest

from app.core.qa_context_config import ContextSettings
from app.services.qa_evidence_gate import EvidenceGate
from app.services.qa_references import reference_lookups
from app.schemas.qa_evidence import RerankedCandidate
from tests.qa_fixtures import INSTRUMENT, identity
from tests.test_qa_context import FakeCatalog, chunk, expand, graph, node
from tests.test_qa_references import reference_graph


def scoped_graph(*, sibling_text="(2) The Authority may file a separate claim under section 99."):
    catalog, reranked = graph(reference=True)
    root = catalog.nodes[identity("target")]
    root.heading, root.own_text = "Powers.", "4. Powers."
    first = node("scope-one", parent=root.id, kind="subsection", label="1",
                 text="(1) A complete power applies only with a signed permit.")
    second = node("scope-two", parent=root.id, kind="subsection", label="2", text=sibling_text)
    first.ordinal, second.ordinal = 20, 21
    part = catalog.chunks[identity("target-chunk")]
    part.chunk_text = root.own_text + "\n" + first.own_text + "\n" + second.own_text
    catalog.nodes.update({first.id:first, second.id:second})
    catalog.members[part.id] = {root.id, first.id, second.id}
    catalog.references[identity("provision-1")][0].target_reference_path = first.reference_path
    catalog._index_graph()
    return catalog, reranked, root, first, second


def text_ids(context):
    return {unit.provision_id for unit in context.units if unit.source_kind == "provision"}


def test_explicit_subsection_keeps_its_full_text_and_parent_without_unrelated_reference_chain():
    catalog, reranked, root, first, second = scoped_graph()
    context = expand(catalog, reranked)
    assert EvidenceGate().decide(context).outcome == "ready_for_prompt"
    assert {first.id, root.id} <= text_ids(context)
    assert second.id not in text_ids(context)
    assert not any(u.chunk_id == identity("target-chunk") for u in context.units)
    assert next(u.text for u in context.units if u.provision_id == first.id) == first.own_text
    assert any(link.target_provision_id == first.id for link in context.reference_links)


@pytest.mark.parametrize("text", [
    "Provided that the signed permit must remain valid.",
    "Unless the permit has expired, the original power continues.",
    "Nothing in this section permits a search without notice.",
    "Notwithstanding any other clause, written consent is required.",
    "The Authority may delegate its powers with prior written approval.",
    "No prior notice shall be required for the separate inquiry.",
    "An order under sub-section (1) may be appealed within thirty days.",
])
def test_possible_section_wide_qualifiers_and_back_references_remain_complete(text):
    catalog, reranked, _, first, _ = scoped_graph(sibling_text=text)
    result = expand(catalog, reranked)
    assert EvidenceGate().decide(result).outcome == "ready_for_prompt"
    assert any(text in u.text for u in result.units)
    assert any(first.own_text in u.text for u in result.units)


def test_target_subtree_includes_its_children_and_a_parent_level_explanation():
    catalog, reranked, root, first, _ = scoped_graph()
    child = node("scope-child", parent=first.id, kind="paragraph", label="a", text="The permit must be in writing.")
    explanation = node("scope-explanation", parent=root.id, kind="other", heading="Explanation",
                       text="Explanation: The requirement includes renewed permits.")
    catalog.nodes.update({child.id:child, explanation.id:explanation})
    catalog.members[identity("target-chunk")].add(child.id)
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert {first.id, child.id, explanation.id, root.id} <= text_ids(result)
    assert EvidenceGate().decide(result).outcome == "ready_for_prompt"


def test_an_appeal_subsection_also_pulls_in_the_subsection_it_refers_back_to():
    catalog, reranked, _, first, second = scoped_graph(
        sibling_text="(2) An order under sub-section (1) may be appealed within thirty days.")
    catalog.references[identity("provision-1")][0].target_reference_path = second.reference_path
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert {first.id, second.id} <= text_ids(result)


def test_a_qualifier_cannot_hide_its_missing_dependency():
    catalog, reranked, _, _, _ = scoped_graph(
        sibling_text="Provided that the exception in section 99 applies.")
    result = expand(catalog, reranked)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"
    assert any(i.code == "unresolved_reference" and i.critical for i in result.issues)


@pytest.mark.parametrize("fault", ["operative_parent", "unknown_child", "ambiguous_selector"])
def test_uncertain_legal_scope_retains_the_existing_full_section_behavior(fault):
    catalog, reranked, root, _, second = scoped_graph()
    if fault == "operative_parent":
        root.own_text += " All powers require the following conditions."
    elif fault == "unknown_child":
        odd = node("scope-unknown", parent=root.id, kind="other", text="A further governing condition.")
        catalog.nodes[odd.id] = odd
    else:
        second.own_text = "The foregoing subsections are subject to section 99."
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert any(u.chunk_id == identity("target-chunk") for u in result.units)
    if fault != "ambiguous_selector":
        assert EvidenceGate().decide(result).outcome == "insufficient_evidence"


@pytest.mark.parametrize("missing", ["scope-one", "target", "scope-qualification"])
def test_missing_scoped_source_or_qualification_proof_is_critical(missing):
    catalog, reranked, root, _, _ = scoped_graph()
    qualifier = node("scope-qualification", parent=root.id, kind="other", heading="Proviso",
                     text="Provided that the signed permit is valid.")
    catalog.nodes[qualifier.id] = qualifier
    catalog.missing.add(identity(missing))
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"
    assert any(i.code == "source_missing" and i.critical for i in result.issues)


def test_scoping_does_not_bypass_missing_published_root_fragments():
    catalog, reranked, _, _, _ = scoped_graph()
    catalog.chunks[identity("target-chunk")].root_total_chunks = 2
    result = expand(catalog, reranked)
    assert any(i.code == "root_fragment_missing" and i.critical for i in result.issues)


def test_later_whole_section_reference_is_not_satisfied_by_a_scoped_cache_entry():
    catalog, reranked, root, _, _ = scoped_graph()
    catalog.references[identity("provision-1")].append(Row(resolution_status="resolved",
        target_instrument_id=INSTRUMENT, target_reference_path=root.reference_path))
    result = expand(catalog, reranked)
    assert any(u.chunk_id == identity("target-chunk") for u in result.units)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"


def test_scoped_group_budget_failure_keeps_no_partial_target_or_audit_link():
    catalog, reranked, root, first, _ = scoped_graph(
        sibling_text="Provided that written consent is required.")
    child = node("scope-extra-child", parent=first.id, kind="paragraph", label="a", text="Another complete condition.")
    catalog.nodes[child.id] = child
    catalog._index_graph()
    result = expand(catalog, reranked, settings=ContextSettings(max_units=5))
    assert any(i.code == "budget_omission" and i.critical for i in result.issues)
    assert not ({root.id, first.id, child.id} & text_ids(result))
    assert not any(link.target_provision_id == first.id for link in result.reference_links)


@pytest.mark.parametrize("heading", ["Proviso", "Explanation"])
def test_nonoperative_qualification_in_a_scoped_target_still_blocks(heading):
    catalog, reranked, root, _, _ = scoped_graph()
    qualifier = node("scope-nonoperative", parent=root.id, kind="other", heading=heading,
                     text="A qualification must not silently disappear.")
    qualifier.source_status = "repealed"
    catalog.nodes[qualifier.id] = qualifier
    catalog._index_graph()
    result = expand(catalog, reranked)
    assert any(i.code == "qualification_missing" and i.critical for i in result.issues)


def list_reference_graph(*, declaration="The Government may make rules consistent with this Act."):
    catalog, reranked = reference_graph(declaration, raw=None)
    root = catalog.nodes[identity("provision-1")]
    first = node("independent-rule", parent=root.id, kind="subsection", label="18",
                 text="(18) for regulating confinement under section 4;")
    foreign = node("foreign-rule", parent=root.id, kind="subsection", label="26",
                   text="(26) for extending the provisions to places appointed under section 541 of the Foreign Code;")
    catalog.nodes.update({first.id:first, foreign.id:foreign})
    catalog.members[identity("chunk-1")].update({first.id,foreign.id})
    catalog._index_graph()
    return catalog, reranked, first, foreign


def test_bare_reference_in_an_explicit_current_act_rule_list_has_local_scope():
    catalog, _, first, foreign = list_reference_graph()
    plans = reference_lookups(first, INSTRUMENT, catalog)
    assert len(plans) == 1 and plans[0].problem is None
    assert plans[0].target_reference_path == catalog.nodes[identity("lookup-4")].reference_path
    assert reference_lookups(foreign, INSTRUMENT, catalog)[0].problem is not None


@pytest.mark.parametrize("fault", ["no_anchor", "foreign_ancestor", "governing_sibling", "ambiguous_act"])
def test_rule_list_scope_never_overrides_external_or_ambiguous_context(fault):
    catalog, _, first, foreign = list_reference_graph()
    if fault == "no_anchor":
        catalog.nodes[identity("provision-1")].own_text = "The following rules may be made."
    elif fault == "foreign_ancestor":
        catalog.nodes[identity("part")].own_text = "The Foreign Act governs these provisions."
    elif fault == "governing_sibling":
        foreign.own_text = "References in the preceding clauses are to the Foreign Act."
    else:
        first.own_text = "(18) for regulating confinement under section 4 of the Act;"
    assert reference_lookups(first, INSTRUMENT, catalog)[0].problem is not None


def test_conduct_declared_to_be_offences_is_not_misread_as_another_statute():
    catalog, _, first, foreign = list_reference_graph(
        declaration="The following acts are declared to be prison-offences when committed by a prisoner.")
    catalog.nodes.pop(foreign.id)
    catalog._index_graph()
    assert reference_lookups(first, INSTRUMENT, catalog)[0].problem is None


def test_generic_prison_regulation_is_not_misread_as_a_named_external_instrument():
    catalog, _, first, foreign = list_reference_graph(
        declaration="The following acts are declared to be prison-offences when committed by a prisoner.")
    catalog.nodes.pop(foreign.id)
    first.own_text = "(1) disobedience to any regulation of the prison declared by rules made under section 4;"
    catalog._index_graph()
    assert reference_lookups(first, INSTRUMENT, catalog)[0].problem is None


def test_a_named_prison_regulation_with_a_year_is_still_external_scope():
    catalog, _, first, _ = list_reference_graph(declaration=(
        "The Government may make rules consistent with this Act under any Regulation of the Prison, 1980."))
    assert reference_lookups(first, INSTRUMENT, catalog)[0].problem is not None


@pytest.mark.parametrize("selector", ["sub-sections (1) to (2)", "sub-sections 1 or 2",
    "sub-section (1)[2]", "paragraph (1)", "sub-section (1) of section 99"])
def test_unsupported_or_mixed_local_selector_scope_uses_the_full_section(selector):
    catalog, reranked, _, first, second = scoped_graph(sibling_text=f"An exception under {selector} applies.")
    result = expand(catalog, reranked)
    assert any(u.chunk_id == identity("target-chunk") for u in result.units)
    assert any(first.own_text in u.text and second.own_text in u.text for u in result.units)


def test_an_implicit_condition_stays_even_without_a_qualifier_keyword():
    text = "(2) The annual licence remains valid for one year."
    catalog, reranked, _, _, _ = scoped_graph(sibling_text=text)
    assert any(text in u.text for u in expand(catalog, reranked).units)


def test_a_nested_target_split_across_chunks_retains_its_entire_provision_text():
    catalog, reranked, root, first, second = scoped_graph()
    part = catalog.chunks[identity("target-chunk")]
    split = len(first.own_text)//2
    part.root_total_chunks = 2
    part.chunk_text = root.own_text + first.own_text[:split]
    extra = chunk("scope-continuation", root, seq=2, total=2,
                  text=first.own_text[split:] + second.own_text)
    catalog.chunks[extra.id] = extra
    catalog.members[extra.id] = {first.id,second.id}
    catalog._index_graph()
    result = expand(catalog,reranked)
    assert EvidenceGate().decide(result).outcome == "ready_for_prompt"
    assert any(u.provision_id==first.id and u.text==first.own_text for u in result.units)


def test_a_proviso_inside_an_otherwise_independent_power_cannot_be_excluded():
    catalog, reranked, _, _, second = scoped_graph()
    proviso = node("scope-nested-proviso", parent=second.id, kind="other", label=None,
                   heading="Proviso", text="Provided that consent is required.")
    catalog.nodes[proviso.id] = proviso
    catalog._index_graph()
    result = expand(catalog,reranked)
    assert any(u.provision_id==proviso.id and u.text==proviso.own_text for u in result.units)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"


def test_explicit_nested_reference_still_obeys_the_configured_depth_limit():
    catalog, reranked, _, first, _ = scoped_graph()
    result = expand(catalog,reranked,settings=ContextSettings(max_reference_depth=0))
    assert any(i.code=="reference_depth_limit" and i.critical for i in result.issues)
    assert first.id not in text_ids(result)
    assert not any(link.target_provision_id==first.id for link in result.reference_links)


def test_a_full_section_selected_by_retrieval_is_never_narrowed():
    catalog, reranked, root, _, _ = scoped_graph()
    part = catalog.chunks[identity("target-chunk")]
    candidate = reranked.candidates[0].candidate.model_copy(update={"chunk_id":part.id,
        "root_provision_id":root.id,"chunk_text":part.chunk_text,"label":root.label,
        "heading":root.heading,"reference_path":root.reference_path})
    selected = RerankedCandidate(publication_id=reranked.retrieval.publication_id,
        candidate=candidate,reranker_score=1,reranker_rank=2)
    reranked = reranked.model_copy(update={"candidates":(*reranked.candidates,selected),
        "retrieval":reranked.retrieval.model_copy(update={"results":[*reranked.retrieval.results,candidate]})})
    result = expand(catalog,reranked)
    assert any(u.chunk_id==part.id and u.relation=="retrieved" for u in result.units)
    assert EvidenceGate().decide(result).outcome == "insufficient_evidence"


def test_optional_definition_keeps_full_group_policy_and_rolls_back_its_failure():
    catalog, reranked, _, first, _ = scoped_graph()
    catalog.references[identity("provision-1")] = []
    defined = node("scope-definition",label="7",heading="Interpretation and definitions",
                   text="A term has the meaning assigned to its referenced condition.")
    part = chunk("scope-definition-chunk",defined,text=defined.own_text)
    catalog.nodes[defined.id],catalog.chunks[part.id] = defined,part
    catalog.members[part.id] = {defined.id}
    catalog.references[defined.id] = [Row(resolution_status="resolved",target_instrument_id=INSTRUMENT,
        target_reference_path=first.reference_path)]
    catalog.missing.add(first.id)
    catalog._index_graph()
    result = expand(catalog,reranked)
    assert EvidenceGate().decide(result).outcome == "ready_for_prompt"
    assert first.id not in text_ids(result)
    assert not any(u.chunk_id==part.id for u in result.units)
    assert not any(link.target_provision_id==first.id for link in result.reference_links)
    assert any(i.code=="optional_context_omitted" for i in result.issues)


def test_failed_optional_whole_group_preserves_a_shared_required_scoped_group():
    catalog, reranked, _, first, _ = scoped_graph()
    defined = node("scope-shared-definition",label="7",heading="Interpretation and definitions",
                   text="A term has the meaning assigned to its referenced condition.")
    part = chunk("scope-shared-definition-chunk",defined,text=defined.own_text)
    catalog.nodes[defined.id],catalog.chunks[part.id] = defined,part
    catalog.members[part.id] = {defined.id}
    catalog.references[defined.id] = [Row(resolution_status="resolved",target_instrument_id=INSTRUMENT,
        target_reference_path=first.reference_path)]
    catalog._index_graph()
    result = expand(catalog,reranked)
    assert EvidenceGate().decide(result).outcome == "ready_for_prompt"
    assert first.id in text_ids(result)
    assert any(link.target_provision_id==first.id and link.source_provision_id==identity("provision-1")
               for link in result.reference_links)
    assert not any(u.chunk_id==part.id for u in result.units)
    assert any(i.code=="optional_context_omitted" for i in result.issues)


@pytest.mark.parametrize("address", ["4(2)", "4(2)(a)"])
def test_same_section_numbered_dependencies_do_not_consume_an_extra_depth(address):
    catalog, reranked, root, first, second = scoped_graph(
        sibling_text="(2) The Authority may grant a separate permit in writing.")
    first.own_text=f"(1) Apply section {address} of this Act before exercising the power."
    if address.endswith('(a)'):
        inner=node("scope-inner-target",parent=second.id,kind="paragraph",label="a",
                   text="The permit is valid only after written consent.")
        catalog.nodes[inner.id]=inner
        catalog.members[identity("target-chunk")].add(inner.id)
    catalog._index_graph()
    result=expand(catalog,reranked,settings=ContextSettings(max_reference_depth=1))
    assert EvidenceGate().decide(result).outcome=="ready_for_prompt"
    assert {first.id,second.id} <= text_ids(result)
    assert not any(i.code=="reference_depth_limit" and i.critical for i in result.issues)
