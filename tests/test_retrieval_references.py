"""Reading provision references out of free-form questions."""
import pytest

from app.retrieval.references import parse_references, schedule_number


def shown(text):
    parsed = parse_references(text)
    return [ref.display for ref in parsed.references], [item.display for item in parsed.ranges]


@pytest.mark.parametrize("text,expected", [
    ("What is Section 3 of Punjab Prevention of Gambling Ordinance 1978?", ["Section 3"]),
    ("what is sec 3 of gambling ordinance", ["Section 3"]),
    ("What is S. 3 PPGO 1978?", ["Section 3"]),
    ("u/s 5 of the arms ordinance", ["Section 5"]),
    ("s.5-A gambling ordinance", ["Section 5A"]),
    ("What does sction 5 of the Ordinance say?", ["Section 5"]),
    ("sectoin 19 consumer protection", ["Section 19"]),
    ("sectionn 7 of borstal act", ["Section 7"]),
    ("Explain section three of the Ordinance", ["Section 3"]),
    ("What is Sectoin １９ of the Consumer Protection Act?", ["Section 19"]),
    ("Section 19A of the Consumer Protection Act", ["Section 19A"]),
    ("dafa 3 gambling ordinance kya hai", ["Section 3"]),
    ("rule 5 of the rules", ["Rule 5"]),
    ("article 5 of the constitution", ["Article 5"]),
    ("art. 19", ["Article 19"]),
    ("he was sentenced to 3 years under section 5", ["Section 5"]),
])
def test_single_references_with_abbreviations_typos_and_spelt_numbers(text, expected):
    assert shown(text) == (expected, [])


@pytest.mark.parametrize("text,expected", [
    ("section 3 and 4 of the gambling ordinance", ["Section 3", "Section 4"]),
    ("sections 3, 4 and 5", ["Section 3", "Section 4", "Section 5"]),
    ("sections 3, 4, and 5-A of the ordinance", ["Section 3", "Section 4", "Section 5A"]),
    ("sections 3 or 4", ["Section 3", "Section 4"]),
    ("Section 9 and Section 10 of the Prisons Act", ["Section 9", "Section 10"]),
])
def test_lists_keep_every_member(text, expected):
    assert shown(text)[0] == expected


@pytest.mark.parametrize("text", ["What are sections 3 to 5 of the Ordinance?", "sections 3-5 gambling",
                                  "sections 3 through 5", "section 3 till 5"])
def test_ranges_are_kept_as_ranges(text):
    assert shown(text) == ([], ["Sections 3 to 5"])


@pytest.mark.parametrize("text,expected", [
    ("Section 4(2)(a) of the Consumer Protection Act", "Section 4(2)(a)"),
    ("sub-section (2) of section 5 of the Prisons Act", "Section 5(2)"),
    ("clause (b) of sub-section (1) of section 3", "Section 3(1)(b)"),
    ("subsection 2 of section 19", "Section 19(2)"),
])
def test_nested_addresses_keep_their_children(text, expected):
    assert shown(text)[0] == [expected]


@pytest.mark.parametrize("text,expected", [
    ("first schedule of the goondas ordinance", "Schedule 1"), ("Schedule II", "Schedule 2"),
    ("second schedule", "Schedule 2"), ("schedule 3", "Schedule 3"),
    ("the schedule to the arms ordinance", "the Schedule"),
])
def test_schedules_in_any_numbering_style(text, expected):
    assert shown(text)[0] == [expected]
    assert schedule_number("FIRST") == schedule_number("I") == schedule_number("1") == "1"


@pytest.mark.parametrize("text", [
    "is it punishable with 3 years imprisonment", "what is the punishment for gambling in a public place?",
    "s 3 years ago", "in 1978 the ordinance", "As per the rules, what is allowed", "this section applies when",
    "which section deals with gaming houses", "the act says nothing", "",
])
def test_ordinary_numbers_and_words_are_not_references(text):
    assert not parse_references(text)


def test_units_stop_a_list_and_spans_cover_what_was_read():
    assert shown("section 3 or 4 years") == (["Section 3"], [])
    parsed = parse_references("What is Section 3 of the Ordinance?")
    start, end = parsed.spans[0]
    assert "What is Section 3 of the Ordinance?"[start:end] == "Section 3"


def test_lists_may_mix_single_numbers_and_ranges():
    parsed = parse_references("the liabilities provided in sections 4 to 8, 11, 13, 14, 16, 18 to 22, he shall")
    assert [(item.start, item.end) for item in parsed.ranges] == [("4", "8"), ("18", "22")]
    assert [item.label for item in parsed.references] == ["11", "13", "14", "16"]
    mixed = parse_references("sections 3 to 5 and 7 of the Act")
    assert [(item.start, item.end) for item in mixed.ranges] == [("3", "5")]
    assert [item.label for item in mixed.references] == ["7"]
    # A sentence length is not the end of a range.
    sentence = parse_references("is the punishment under section 7 to 10 years")
    assert [item.label for item in sentence.references] == ["7"] and not sentence.ranges


def test_urdu_decimal_labels_identify_the_same_provision_as_ascii():
    assert parse_references("دفعہ ۳").references == parse_references("section 3").references
