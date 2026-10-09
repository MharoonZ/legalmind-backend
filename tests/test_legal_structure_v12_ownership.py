"""Opposing ownership cases distilled from the corpus audit, not PDF-specific rules."""

from dataclasses import replace
from types import SimpleNamespace as Row

from tests.test_legal_structure_unseen_regressions import assert_source_exact, one, source

from app.services.legal_audit import structure_findings
from app.services.legal_structure import parse_structure


def test_caption_wrapped_after_section_heading_stays_with_section():
    document = source([(110, "13. Persons found to be over eighteen years not to be detained in Reformatory\n"
                             "Schools.\u2013 (1) The Committee shall report the case.\n"
                             "(2) No person shall be detained afterwards.\n")])
    parsed = parse_structure([document])
    section = one(parsed, "section", "13")
    first = one(parsed, "subsection", "1")
    assert "Schools.\u2013" in section.own_text
    assert "Reformatory Schools" in (section.heading or "")
    assert "Schools.\u2013" not in first.own_text
    assert first.own_text.startswith("(1)")
    assert_source_exact(parsed, document)


def test_amendment_prefixed_marker_only_heading_keeps_caption():
    document = source([(110, "2[26. \nConsumer Courts.\u2013 (1) A Court may be designated.\n"
                             "(2) The Court may make orders.\n")])
    parsed = parse_structure([document])
    section = one(parsed, "section", "26")
    first = one(parsed, "subsection", "1")
    assert "Consumer Courts.\u2013" in section.own_text
    assert section.heading == "Consumer Courts"
    assert first.own_text.startswith("(1)")
    assert_source_exact(parsed, document)


def test_run_in_second_subsection_keeps_its_own_caption():
    document = source([(110, "22. Guardianship.\u2013 (1) A school is appointed.\n\n"
                             "Power to apprentice youthful offender.\u2013 (2) A licence may be given.\n")])
    parsed = parse_structure([document])
    assert "Power to apprentice" in one(parsed, "subsection", "2").own_text
    assert "Power to apprentice" not in one(parsed, "section", "22").own_text
    assert_source_exact(parsed, document)


def test_post_list_court_may_returns_to_parent_across_blocks_and_page():
    document = source(
        [(110, "44. Commission.\u2013 In any of the following cases:\n"
               "(a) first case;\n(b) second case;\n(c) third case for an order for\n")],
        [(110, "removal;\nthe Court may, if it thinks fit, issue a commission.\n"
               "45. Next section.\u2013 A different provision.\n")],
    )
    parsed = parse_structure([document])
    assert "the Court may" in one(parsed, "section", "44").own_text
    assert "the Court may" not in one(parsed, "paragraph", "c").own_text
    assert "removal;" in one(parsed, "paragraph", "c").own_text
    assert_source_exact(parsed, document)


def test_independent_penalty_for_every_list_item_stays_with_each_item():
    document = source(
        [(110, "10. Penalty.\u2013 (1) Whoever employs any of the following:\n"
               "(a) a person to solicit alms, shall be punished with imprisonment; or\n"
               "(b) more than one person to solicit alms, shall\n"),
         (260, "be punished with imprisonment for five years; or\n"
               "(c) a child to solicit alms, shall be punished with imprisonment.\n")],
    )
    parsed = parse_structure([document])
    parent = one(parsed, "subsection", "1")
    second = one(parsed, "paragraph", "b")
    assert "shall" in second.own_text and "be punished" in second.own_text
    assert "be punished" not in parent.own_text
    assert_source_exact(parsed, document)


def test_shared_penalty_after_list_returns_to_parent():
    document = source([(110, "17. Offences.\u2013 (1) Whoever:\n"
                             "(a) keeps a prohibited object; or\n"
                             "(b) transfers that object, shall be punished with imprisonment.\n")])
    parsed = parse_structure([document])
    parent = one(parsed, "subsection", "1")
    assert "shall be punished" in parent.own_text
    assert "shall be punished" not in one(parsed, "paragraph", "b").own_text
    assert_source_exact(parsed, document)


def test_short_law_title_running_header_in_mixed_block_is_not_operative_text():
    document = source(
        [(110, "6. Officers.\u2013 Every officer shall discharge the same duties as a Jailer.\n")],
        [(69, "The Prisons Act 1894 (IX of 1894) \n7\n\npowers shall continue.\n"
              "7. Next provision.\u2013 A separate duty applies.\n")],
    )
    document = replace(document, title="The Prisons Act, 1894")
    parsed = parse_structure([document])
    section = one(parsed, "section", "6")
    assert "powers shall continue" in section.own_text
    assert "The Prisons Act 1894" not in section.own_text
    assert any(node.kind == "other" and node.heading == "Running header"
               and "The Prisons Act 1894" in node.own_text for node in parsed.nodes)
    assert_source_exact(parsed, document)


def test_law_title_inside_prose_is_not_discarded_as_page_header():
    document = source([(220, "6. Citation.\u2013 The Prisons Act 1894 (IX of 1894) is cited here.\n")])
    document = replace(document, title="The Prisons Act, 1894")
    parsed = parse_structure([document])
    assert "The Prisons Act 1894" in one(parsed, "section", "6").own_text
    assert_source_exact(parsed, document)


def _audit_node(identifier, kind, label, own_text, parent_id=None, ordinal=1):
    return Row(id=identifier, parent_id=parent_id, kind=kind, label=label,
               own_text=own_text, heading=None, reference_path=identifier,
               ordinal=ordinal, source_status=None)


def _codes(nodes, spans=()):
    return {item["code"] for item in structure_findings(nodes, spans)}


def test_auditor_flags_caption_moved_to_first_subsection_but_not_valid_caption():
    wrong = [_audit_node("s", "section", "13", "13. Persons found to be over eighteen years", ordinal=1),
             _audit_node("sub", "subsection", "1", "Schools.\u2013 (1) A person shall report.", "s", 2)]
    assert "section_caption_in_first_subsection" in _codes(wrong)
    marker_only = [_audit_node("s", "section", "26", "2[26. \n", ordinal=1),
                   _audit_node("sub", "subsection", "1", "Consumer Courts.\u2013 (1) A Court is designated.", "s", 2)]
    assert "section_caption_in_first_subsection" in _codes(marker_only)
    right = [_audit_node("s", "section", "13", "13. Persons found to be over eighteen years Schools.\u2013 "),
             _audit_node("sub", "subsection", "1", "(1) A person shall report.", "s", 2)]
    assert "section_caption_in_first_subsection" not in _codes(right)


def test_auditor_flags_item_penalty_moved_to_parent_but_not_shared_penalty():
    wrong = [_audit_node("s", "section", "10", "10. Penalty.\u2013 "),
             _audit_node("sub", "subsection", "1", "(1) Whoever lists these, shall be punished.", "s", 2),
             _audit_node("a", "paragraph", "a", "(a) first act, shall be punished; or", "sub", 3),
             _audit_node("b", "paragraph", "b", "(b) second act,", "sub", 4)]
    assert "item_penalty_moved_to_parent" in _codes(wrong)
    right = [_audit_node("s", "section", "17", "17. Offences.\u2013 "),
             _audit_node("sub", "subsection", "1", "(1) Whoever lists these, shall be punished.", "s", 2),
             _audit_node("a", "paragraph", "a", "(a) first act; or", "sub", 3),
             _audit_node("b", "paragraph", "b", "(b) second act,", "sub", 4)]
    assert "item_penalty_moved_to_parent" not in _codes(right)


def test_auditor_flags_running_header_in_legal_span_but_not_separate_header():
    title = "The Example Ordinance 2018 (XX of 2018) \n5\n"
    body = "(3) The report shall be sent.\n"
    block = Row(normalized_text=body + title, bbox={"y0": 85}, block_kind="paragraph")
    span = Row(provision_id="sub", provision_char_start=0, provision_char_end=len(body + title),
               block_char_start=0, block_char_end=len(body + title), mapping_quality="exact")
    mixed = [_audit_node("sub", "subsection", "3", body + title)]
    assert "running_header_in_operative_text" in _codes(mixed, [(span, block)])
    separate = [_audit_node("sub", "subsection", "3", body)]
    body_span = Row(provision_id="sub", provision_char_start=0, provision_char_end=len(body),
                    block_char_start=0, block_char_end=len(body), mapping_quality="exact")
    assert "running_header_in_operative_text" not in _codes(separate, [(body_span, block)])
