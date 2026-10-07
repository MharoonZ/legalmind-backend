"""Cross-law candidates remain exact-source proposals, not legal conclusions."""

from uuid import uuid4

from app.services.legal_relationships import extract_relation_candidates
from app.services.legal_structure import DraftNode, DraftSpan, ParsedStructure


def _node(parsed: ParsedStructure, label: str, text: str, chunks: tuple[str, ...],
          *, ordinal: int) -> DraftNode:
    assert "".join(chunks) == text
    node = DraftNode(id=uuid4(), parent_id=None, kind="section", label=label,
                     reference_path=f"s:{label}", ordinal=ordinal,
                     heading=text.split(".–", 1)[0], own_text=text)
    parsed.nodes.append(node)
    offset = 0
    for chunk in chunks:
        parsed.spans.append(DraftSpan(
            provision_id=node.id, extraction_id=uuid4(), page_id=uuid4(),
            block_id=uuid4(), provision_char_start=offset,
            provision_char_end=offset + len(chunk),
            block_char_start=0, block_char_end=len(chunk),
        ))
        offset += len(chunk)
    return node


def test_borstal_section_32_proposes_both_acts_and_all_external_references():
    first = (
        "32. Application to Borstal Institution of certain provisions of the Prisons Act, 1894; "
        "and the Prisoners Act, 1900.– Subject to the provisions of section 33 of this Act, "
        "the provisions of section 12 and Chapter XI of the Prisons Act, 189415 and of "
        "sections 35 to 50 (inclusive) and the rules made under section 51 of\n"
    )
    second = (
        "the Prisoners Act, 19001, shall apply as far as may be to Borstal Institutions "
        "established under this Act."
    )
    parsed = ParsedStructure()
    node = _node(parsed, "32", first + second, (first, second), ordinal=1)
    candidates = extract_relation_candidates(parsed)
    assert {candidate.target_title.casefold() for candidate in candidates} == {
        "the prisons act, 1894", "the prisoners act, 1900",
    }
    refs = {(candidate.target_title.casefold(), candidate.target_reference)
            for candidate in candidates if candidate.target_reference}
    assert refs == {
        ("the prisons act, 1894", "section 12"),
        ("the prisons act, 1894", "Chapter XI"),
        ("the prisoners act, 1900", "sections 35 to 50"),
        ("the prisoners act, 1900", "section 51"),
    }
    assert all(candidate.relation_kind == "cites" for candidate in candidates)
    assert all(candidate.source_provision_id == node.id for candidate in candidates)
    assert all(candidate.evidence_text == node.own_text[
        candidate.source_char_start:candidate.source_char_end]
        for candidate in candidates)
    assert all(candidate.source_span.provision_char_start <= candidate.source_char_start
               < candidate.source_char_end <= candidate.source_span.provision_char_end
               for candidate in candidates)


def test_extension_amendment_repeal_have_separate_source_evidence():
    parsed = ParsedStructure()
    section_2 = _node(
        parsed, "2",
        "2. Extension of Act II of 1947 to the whole of West Pakistan.– "
        "The Prevention of Corruption Act, 1947, shall extend to the whole Province.",
        ("2. Extension of Act II of 1947 to the whole of West Pakistan.– "
         "The Prevention of Corruption Act, 1947, shall extend to the whole Province.",),
        ordinal=1,
    )
    section_3 = _node(
        parsed, "3", "3. Amendment of Act II of 1947.– In the said Act, references are changed.",
        ("3. Amendment of Act II of 1947.– In the said Act, references are changed.",),
        ordinal=2,
    )
    section_4 = _node(
        parsed, "4", "4. Repeal.– The Bahawalpur Prevention of Corruption Act, 1950, is hereby repealed.",
        ("4. Repeal.– The Bahawalpur Prevention of Corruption Act, 1950, is hereby repealed.",),
        ordinal=3,
    )
    candidates = extract_relation_candidates(parsed)
    assert {(item.source_provision_id, item.relation_kind, item.target_title) for item in candidates} == {
        (section_2.id, "extends", "The Prevention of Corruption Act, 1947"),
        (section_3.id, "amends", "The Prevention of Corruption Act, 1947"),
        (section_4.id, "repeals", "The Bahawalpur Prevention of Corruption Act, 1950"),
    }
    amendment = next(item for item in candidates if item.source_provision_id == section_3.id)
    assert amendment.evidence_text == "Act II of 1947"
    assert amendment.link_basis == "defined_short_citation"


def test_source_note_titles_do_not_become_operative_relationships():
    parsed = ParsedStructure()
    note_text = "1The Prisons Act, 1894 was amended later."
    note = DraftNode(id=uuid4(), parent_id=None, kind="other", label="1",
                     reference_path="note:1", ordinal=1, heading="Source note 1",
                     own_text=note_text)
    parsed.nodes.append(note)
    parsed.spans.append(DraftSpan(
        provision_id=note.id, extraction_id=uuid4(), page_id=uuid4(), block_id=uuid4(),
        provision_char_start=0, provision_char_end=len(note_text),
        block_char_start=0, block_char_end=len(note_text),
    ))
    assert extract_relation_candidates(parsed) == []


def test_current_instrument_title_is_not_cross_instrument_relationship():
    parsed = ParsedStructure()
    text = "1. The Punjab Borstal Act, 1926 applies as stated."
    _node(parsed, "1", text, (text,), ordinal=1)
    assert extract_relation_candidates(
        parsed, source_titles={"The Punjab Borstal Act, 1926"},
    ) == []


def test_repeal_in_short_title_is_not_self_repeal_but_operative_repeal_remains():
    parsed = ParsedStructure()
    short_title = "1. This Act may be called the Public Gambling (Repeal) Act 1985."
    _node(parsed, "1", short_title, (short_title,), ordinal=1)
    operative = "2. Repeal. The Public Gambling Act, 1867 is hereby repealed."
    _node(parsed, "2", operative, (operative,), ordinal=2)
    candidates = extract_relation_candidates(
        parsed, source_titles={"The Public Gambling (Repeal) Act, 1985"},
    )
    assert [(item.target_title, item.relation_kind) for item in candidates] == [
        ("The Public Gambling Act, 1867", "repeals"),
    ]
