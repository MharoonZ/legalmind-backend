"""Locating quotes in the published text and numbering citations."""
from dataclasses import replace

import pytest

from app.qa.citations import ground_answer, locate_quote, topic_excerpt
from app.qa.context import EvidenceUnit
from tests.corpus import CONSUMER, GAMBLING, PRISONS, law_id, make_snapshot

SECTION_5 = ("5. Penalty for gaming in a public place.– Whoever is found gaming in any public place, \n"
             "street or thoroughfare, \nshall be punishable with imprisonment for a term which may extend to \n\n"
             "1[three years], or with fine which may extend to 2[five thousand rupees], or with both.")


@pytest.fixture(scope="module")
def snapshot():
    return make_snapshot()


def unit(snapshot, key, law, label):
    chunk = snapshot.chunks_by_root[snapshot.find_root(law_id(law), "section", label)][0]
    return EvidenceUnit(key, chunk, "retrieved")


def test_topic_fallback_needs_mapped_operative_text(snapshot):
    evidence = unit(snapshot, "E1", CONSUMER, "19")
    chunk = evidence.chunk
    topic = ["receipt", "purchaser"]
    grounded = topic_excerpt(topic, [evidence], snapshot)
    assert grounded and {c.page_index for c in grounded.citations} == {s.page_index for s in chunk.segments}
    only_one_page = replace(chunk, segments=chunk.segments[:1])
    assert topic_excerpt(topic, [replace(evidence, chunk=only_one_page)], snapshot) is None
    heading_only = replace(chunk, text=chunk.text[:chunk.body_start] + chunk.heading)
    assert topic_excerpt(topic, [replace(evidence, chunk=heading_only)], snapshot) is None


@pytest.mark.parametrize("quote", [
    "Whoever is found gaming in any public place, street or thoroughfare",
    "whoever is found GAMING in any public place street or thoroughfare",          # case, punctuation
    "imprisonment for a term which may extend to three years",                       # footnote number, brackets
    "may extend to [three years], or with fine which may extend to [five thousand rupees]",
    "“Whoever is found gaming in any public place”",                                 # typographic quotes
    "Whoever is found gaming in any public place ... or with both",                  # elision: longest part
    "shall be punishable with imprisonmnet for a term which may extend to three years",  # copying slip
])
def test_quotes_are_found_despite_formatting_differences(quote):
    span = locate_quote(SECTION_5, quote)
    assert span is not None
    start, end = span
    assert 0 <= start < end <= len(SECTION_5) and SECTION_5[start:end].strip()


def test_located_span_is_the_source_wording_not_the_models_wording():
    start, end = locate_quote(SECTION_5, "extend to three years, or with fine")
    assert SECTION_5[start:end] == "extend to \n\n1[three years], or with fine"


@pytest.mark.parametrize("quote", [
    "Whoever is found drinking in any public place shall be fined ten thousand rupees",
    "public", "", "   ", "three",
])
def test_invented_or_trivial_quotes_are_not_located(quote):
    assert locate_quote(SECTION_5, quote) is None


def test_markers_become_numbered_citations_with_page_and_provision(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5"), unit(snapshot, "E2", CONSUMER, "19")]
    grounded = ground_answer(
        "Gaming in a public place is punishable with up to three years [E1]. A receipt must be issued [E2].",
        [{"evidence_id": "E1", "quote": "imprisonment for a term which may extend to three years"},
         {"evidence_id": "E2", "quote": "shall issue to the purchaser a receipt"}], units, snapshot)
    assert grounded.answer == ("Gaming in a public place is punishable with up to three years [1]. "
                               "A receipt must be issued [2].")
    first, second = grounded.citations
    assert (first.law, first.kind, first.label) == (GAMBLING, "section", "5")
    assert first.quote == "imprisonment for a term which may extend to 1[three years]"
    assert first.evidence_id == units[0].evidence_id and first.chunk_id == units[0].chunk.id
    assert first.page_index in {6, 7} and first.publication_id == snapshot.id
    assert second.law == CONSUMER and second.label == "19"
    assert [claim.citation_indexes for claim in grounded.claims] == [(0,), (1,)]
    assert (grounded.verified_quotes, grounded.dropped_quotes, grounded.fallback_citations) == (2, 0, 0)


def test_numbering_follows_first_use_in_the_answer(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5"), unit(snapshot, "E2", CONSUMER, "19")]
    grounded = ground_answer(
        "A receipt is required [E2]. Public gaming is punishable [E1][E2].",
        [{"evidence_id": "E1", "quote": "Whoever is found gaming in any public place"},
         {"evidence_id": "E2", "quote": "shall issue to the purchaser a receipt"}], units, snapshot)
    assert grounded.answer == "A receipt is required [1]. Public gaming is punishable [2][1]."
    assert [citation.label for citation in grounded.citations] == ["19", "5"]


def test_an_unverifiable_quote_is_dropped_but_the_cited_passage_is_still_cited(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5")]
    grounded = ground_answer(
        "Public gaming is punishable with imprisonment which may extend to three years [E1].",
        [{"evidence_id": "E1", "quote": "the offender shall be fined ten lakh rupees and whipped"}],
        units, snapshot)
    assert grounded.dropped_quotes == 1 and grounded.fallback_citations == 1
    assert grounded.answer.endswith("[1].") and len(grounded.citations) == 1
    # The fallback quotes real source text that overlaps the sentence it supports.
    assert "imprisonment" in grounded.citations[0].quote
    assert grounded.citations[0].quote in " ".join(units[0].chunk.text.split())


def test_unknown_evidence_ids_and_stray_markers_are_removed(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5")]
    grounded = ground_answer(
        "Statement one [E9]. Statement two [E1, E7].",
        [{"evidence_id": "E9", "quote": "Whoever is found gaming in any public place"},
         {"evidence_id": "E1", "quote": "Whoever is found gaming in any public place"}], units, snapshot)
    assert grounded.answer == "Statement one. Statement two [1]."
    assert len(grounded.citations) == 1 and grounded.dropped_quotes == 1


def test_markers_are_added_when_the_model_only_listed_quotes(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "3")]
    grounded = ground_answer(
        "Keeping a common gaming house is punishable with imprisonment of one month to one year.\n"
        "It is not necessary to prove that the person was playing for money.",
        [{"evidence_id": "E1", "quote": "shall be punishable with imprisonment for a term which shall not be less than one month"},
         {"evidence_id": "E1", "quote": "it shall not be necessary to prove that the person found playing was playing for any money"}],
        units, snapshot)
    lines = grounded.answer.split("\n")
    assert lines[0].endswith("[1].") and lines[1].endswith("[2].")
    assert len(grounded.citations) == 2


def test_one_passage_with_two_quotes_cites_the_quote_each_sentence_is_about(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "3")]
    grounded = ground_answer(
        "The penalty is imprisonment of one month to one year [E1]. "
        "The prosecution need not prove the person was playing for money [E1].",
        [{"evidence_id": "E1", "quote": "shall be punishable with imprisonment for a term which shall not be less than one month"},
         {"evidence_id": "E1", "quote": "it shall not be necessary to prove that the person found playing was playing for any money"}],
        units, snapshot)
    assert grounded.answer.count("[1]") == 1 and grounded.answer.count("[2]") == 1
    assert "necessary to prove" in grounded.citations[1].quote


def test_duplicate_quotes_bullets_and_public_ids_are_handled(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5")]
    grounded = ground_answer(
        "The collection covers:- Public gaming [E1].- Nothing else.",
        [{"evidence_id": units[0].evidence_id, "quote": "Whoever is found gaming in any public place"},
         {"evidence_id": "[E1]", "quote": "found gaming in any public place"}], units, snapshot)
    assert grounded.answer == "The collection covers:\n- Public gaming [1].\n- Nothing else."
    assert len(grounded.citations) == 1 and grounded.verified_quotes == 2


def test_an_answer_without_evidence_has_no_citations(snapshot):
    grounded = ground_answer("This collection does not cover that.", [], [], snapshot)
    assert grounded.answer == "This collection does not cover that." and not grounded.citations


def test_a_provision_the_answer_names_without_citing_is_linked_to_its_text(snapshot):
    units = [unit(snapshot, "E1", CONSUMER, "19"), unit(snapshot, "E2", CONSUMER, "32")]
    grounded = ground_answer(
        "Yes. Under Section 19 of The Punjab Consumer Protection Act 2005, a trader who sells goods must "
        "issue a receipt to the purchaser. A manufacturer who infringes these liabilities can be imprisoned "
        "for up to two years, as provided by Section 32.",
        [{"evidence_id": "E2", "quote": "he shall be punished with imprisonment which may extend to two years"}],
        units, snapshot)
    assert [citation.label for citation in grounded.citations] == ["19", "32"]
    assert "issue to the purchaser a receipt" in grounded.citations[0].quote
    assert "issue a receipt to the purchaser [1]." in grounded.answer
    assert grounded.answer.endswith("as provided by Section 32 [2].")
    assert (grounded.verified_quotes, grounded.fallback_citations) == (1, 1)


def test_named_provisions_are_not_linked_when_already_cited_or_ambiguous(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "3"), unit(snapshot, "E2", PRISONS, "3")]
    cited = ground_answer(
        "Section 3 of the Punjab Prevention of Gambling Ordinance, 1978 punishes keeping a gaming house [E1].",
        [{"evidence_id": "E1", "quote": "keeps or uses the same as a common gaming-house"}], units, snapshot)
    assert len(cited.citations) == 1 and cited.answer.count("[1]") == 1
    # "Section 3" exists in both cited laws and the sentence names neither: it stays unlinked.
    vague = ground_answer("Section 3 deals with this.", [], units, snapshot)
    assert not vague.citations and vague.answer == "Section 3 deals with this."
    # Naming the law settles it; a law outside the collection is never linked.
    named = ground_answer("Section 3 of the Prisons Act, 1894 defines a prison.", [], units, snapshot)
    assert [(citation.law, citation.label) for citation in named.citations] == [(PRISONS, "3")]
    foreign = ground_answer("Section 3 of the Arms Ordinance 1965 applies.", [], units[:1], snapshot)
    assert not foreign.citations


def test_a_paraphrased_quote_is_replaced_by_the_sentence_it_restates(snapshot):
    units = [unit(snapshot, "E1", CONSUMER, "19")]
    grounded = ground_answer(
        "A receipt must be given.",
        [{"evidence_id": "E1", "quote": "every trader who sells goods must issue a receipt to the purchaser "
                                         "showing the date of sale and the price of the goods"}],
        units, snapshot)
    assert len(grounded.citations) == 1 and grounded.answer == "A receipt must be given [1]."
    assert grounded.citations[0].quote.startswith("Every manufacturer or trader who sells any goods shall")
    assert (grounded.verified_quotes, grounded.dropped_quotes, grounded.fallback_citations) == (0, 0, 1)


def test_abbreviations_do_not_split_an_answer_sentence(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5")]
    grounded = ground_answer(
        "The fine may extend to Rs. 5,000 for gaming in a public place. Nothing else applies.",
        [{"evidence_id": "E1", "quote": "Whoever is found gaming in any public place"}], units, snapshot)
    assert grounded.answer == ("The fine may extend to Rs. 5,000 for gaming in a public place [1]. "
                               "Nothing else applies.")


def test_a_quote_of_only_the_heading_is_replaced_by_the_provisions_wording(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "3")]
    heading = "Penalty for owning or keeping or having charge of a common gaming house"
    grounded = ground_answer(
        "Keeping a common gaming house is punishable with imprisonment of one month to one year [E1]. "
        "The prosecution need not prove that the person was playing for money [E1].",
        [{"evidence_id": "E1", "quote": heading}], units, snapshot)
    # Each statement is tied to the sentence of the section that supports it.
    assert len(grounded.citations) == 2 and "[1]" in grounded.answer and "[2]" in grounded.answer
    assert "imprisonment" in grounded.citations[0].quote and heading not in grounded.citations[0].quote
    assert "necessary to prove" in grounded.citations[1].quote
    assert (grounded.verified_quotes, grounded.dropped_quotes, grounded.fallback_citations) == (0, 0, 2)
    # Without markers the passage is still cited through its wording, not its title.
    unmarked = ground_answer("Keeping a common gaming house is punishable with imprisonment.",
                             [{"evidence_id": "E1", "quote": heading}], units, snapshot)
    assert len(unmarked.citations) == 1 and heading not in unmarked.citations[0].quote
    assert "common gaming-house" in unmarked.citations[0].quote and unmarked.answer.endswith("[1].")


def test_markers_without_quotes_cite_the_sentence_behind_each_statement(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "3"), unit(snapshot, "E2", GAMBLING, "5")]
    grounded = ground_answer(
        "Keeping a gaming house means imprisonment of one month to one year [E1]. "
        "It is not necessary to prove the person was playing for money [E1]. "
        "Gaming in a public place may mean three years [E2].",
        [], units, snapshot)
    assert [citation.label for citation in grounded.citations] == ["3", "3", "5"]
    assert "one month" in grounded.citations[0].quote and "necessary to prove" in grounded.citations[1].quote
    assert grounded.answer.count("[1]") == 1 and grounded.answer.count("[2]") == 1 and grounded.answer.endswith("[3].")
    assert grounded.fallback_citations == 3


# ---------------------------------------------------------------------------
# What a marker supports, where sentences end, and answers that are not in English.

PENALTY = {"evidence_id": "E1",
           "quote": "shall be punishable with imprisonment for a term which shall not be less than one month"}
NO_PROOF = {"evidence_id": "E1",
            "quote": "it shall not be necessary to prove that the person found playing was playing for any money"}
NO_PROOF_FULL = {"evidence_id": "E1", "quote": "In a prosecution under sub-section (1), " + NO_PROOF["quote"]}
PUBLIC = {"evidence_id": "E2", "quote": "Whoever is found gaming in any public place"}
THREE_YEARS = {"evidence_id": "E2", "quote": "imprisonment for a term which may extend to three years"}
FINE_5000 = {"evidence_id": "E2", "quote": "with fine which may extend to five thousand rupees"}
ONE_YEAR = {"evidence_id": "E3", "quote": "imprisonment for a term which may extend to one year, "
                                          "or with fine which may extend to five hundred rupees"}
FINE_500 = {"evidence_id": "E3", "quote": "with fine which may extend to five hundred rupees"}
KEEPING = "Keeping a common gaming house is punishable with imprisonment of one month to one year"
PROVING = "The prosecution need not prove that the person was playing for money"
WAGER = "No proof of a wager is needed"                 # restates NO_PROOF without sharing a word with it
URDU_PUBLIC = "عوامی جگہ پر جوا کھیلنے کی سزا تین سال تک قید ہے"      # public gaming: up to three years in prison
URDU_PROOF = "استغاثہ کو یہ ثابت کرنے کی ضرورت نہیں"                 # the prosecution need not prove this
URDU_3_YEARS = "عوامی جگہ پر جوا کھیلنے کی سزا ۳ سال تک قید ہے"       # the same penalty, the term in Urdu digits
URDU_1_YEAR = "جوئے کے اڈے میں پائے جانے پر ۱ سال تک قید ہے"          # found in a gaming house: up to 1 year
OFFENCES = (
    "7. Offences by companies.– Where an offence under this Act has been committed by a company, every person "
    "who at the time the offence was committed was in charge of, and was responsible to, the company for the "
    "conduct of the business of the company, as well as the company itself, and every director, manager, "
    "secretary or other officer of the company with whose consent or connivance the offence was committed, "
    "shall be deemed to be guilty of the offence and shall be punishable with imprisonment for a term which "
    "may extend to seven years and with fine which may extend to one million rupees.")


def gambling(snapshot):
    """E1 keeping a gaming house (its penalty, then what need not be proved), E2 public gaming, E3 being found in one."""
    return [unit(snapshot, "E1", GAMBLING, "3"), unit(snapshot, "E2", GAMBLING, "5"),
            unit(snapshot, "E3", GAMBLING, "4")]


def check(snapshot, units, answer, drafts, expected, quoted):
    """Ground ``answer``; ``quoted`` gives, in citation order, a few words each citation must quote."""
    grounded = ground_answer(answer, drafts, units, snapshot)
    assert grounded.answer == expected
    assert len(grounded.citations) == len(quoted)
    for citation, part in zip(grounded.citations, quoted):
        assert part in citation.quote
    return grounded


@pytest.mark.parametrize("answer, drafts, expected, quoted", [
    # A marker written after the full stop belongs to the sentence it follows.
    (f"{KEEPING}. [E1] {PROVING}. [E1]", [PENALTY, NO_PROOF],
     f"{KEEPING}. [1] {PROVING}. [2]", ["less than one month", "necessary to prove"]),
    (f"{KEEPING}. [E1] {PROVING}. [E1]", [],
     f"{KEEPING}. [1] {PROVING}. [2]", ["less than one month", "necessary to prove"]),
    (f"- {KEEPING}. [E1]\n- {PROVING}. [E1]", [],
     f"- {KEEPING}. [1]\n- {PROVING}. [2]", ["less than one month", "necessary to prove"]),
    (f"{KEEPING}.\n[E1]\n{PROVING}.\n[E1]", [],
     f"{KEEPING}.\n[1]\n{PROVING}.\n[2]", ["less than one month", "necessary to prove"]),
    # One that opens the answer belongs to the sentence it opens.
    (f"[E1] {PROVING}.", [PENALTY, NO_PROOF], f"[1] {PROVING}.", ["necessary to prove"]),
    # An abbreviation in front of the marker does not cut the statement short.
    ("Keeping a common gaming house is punishable with imprisonment of up to one year or a fine of up to "
     "Rs. 1,000 [E1].", [],
     "Keeping a common gaming house is punishable with imprisonment of up to one year or a fine of up to "
     "Rs. 1,000 [1].", ["one thousand rupees"]),
    (f"{PROVING}, not even Rs. 10 [E1].", [PENALTY, NO_PROOF],
     f"{PROVING}, not even Rs. 10 [1].", ["necessary to prove"]),
    # Markers side by side support the same statement, however they are joined.
    (f"{PROVING} [E2] [E1].", [], f"{PROVING} [1] [2].", ["Whoever is found gaming", "necessary to prove"]),
    (f"{PROVING}, see [E2] and [E1].", [],
     f"{PROVING}, see [1] and [2].", ["Whoever is found gaming", "necessary to prove"]),
    (f"{PROVING}. [E2][E1]", [], f"{PROVING}. [1][2]", ["Whoever is found gaming", "necessary to prove"]),
    (f"{PROVING}. [E2, E1]", [], f"{PROVING}. [1][2]", ["Whoever is found gaming", "necessary to prove"]),
    # Every statement is already linked, so the surplus quote is omitted.
    (f"{PROVING} [E2] [E1].", [PENALTY, NO_PROOF, PUBLIC],
     f"{PROVING} [1] [2].", ["Whoever is found gaming", "necessary to prove"]),
])
def test_a_marker_supports_the_sentence_it_stands_in_or_follows(snapshot, answer, drafts, expected, quoted):
    check(snapshot, gambling(snapshot), answer, drafts, expected, quoted)


@pytest.mark.parametrize("answer, expected", [
    ("Under Section 19 of the Punjab Consumer Protection Act 2005 (Act No. II of 2005), a trader must issue "
     "a receipt.",
     "Under Section 19 of the Punjab Consumer Protection Act 2005 (Act No. II of 2005), a trader must issue "
     "a receipt [1]."),
    ("Section 19 of the Punjab Consumer Protection Act 2005, as read by Mr. A. Khan, requires a receipt.",
     "Section 19 of the Punjab Consumer Protection Act 2005, as read by Mr. A. Khan, requires a receipt [1]."),
    # "i.e." is not the end of the sentence, but the full stop after the amount that follows "Rs." is.
    ("Section 19 of the Punjab Consumer Protection Act 2005 applies to traders, i.e. Sellers of goods, and "
     "requires a receipt for Rs. 500. Nothing else applies.",
     "Section 19 of the Punjab Consumer Protection Act 2005 applies to traders, i.e. Sellers of goods, and "
     "requires a receipt for Rs. 500 [1]. Nothing else applies."),
    ("Under Sec. 19 of the Punjab Consumer Protection Act 2005, read with Sch. I and Khan v. State, a receipt "
     "is required. Nothing else applies.",
     "Under Sec. 19 of the Punjab Consumer Protection Act 2005, read with Sch. I and Khan v. State, a receipt "
     "is required [1]. Nothing else applies."),
])
def test_an_abbreviation_or_an_initial_does_not_end_a_sentence(snapshot, answer, expected):
    # The provision is named, not cited: its link goes where the sentence that names it ends.
    check(snapshot, [unit(snapshot, "E1", CONSUMER, "19")], answer, [], expected, ["issue to the purchaser a receipt"])


@pytest.mark.parametrize("answer, expected", [
    (f"1. {KEEPING}.\n2. {WAGER}.", f"1. {KEEPING} [1].\n2. {WAGER} [2]."),
    (f"12. {KEEPING}.\n13. {WAGER}.", f"12. {KEEPING} [1].\n13. {WAGER} [2]."),
    (f"a. {KEEPING}.\nb. {WAGER}.", f"a. {KEEPING} [1].\nb. {WAGER} [2]."),
    (f"iv. {KEEPING}.\nv. {WAGER}.", f"iv. {KEEPING} [1].\nv. {WAGER} [2]."),
    (f"- 1. {KEEPING}.\n- 2. {WAGER}.", f"- 1. {KEEPING} [1].\n- 2. {WAGER} [2]."),
    (f"**1.** {KEEPING}.\n**2.** {WAGER}.", f"**1.** {KEEPING} [1].\n**2.** {WAGER} [2]."),
    # Numbers inside a line are labels too: the items stay one sentence and no marker follows a number.
    ("The rules are: 1. Keeping a common gaming house is punishable with imprisonment. 2. The prosecution "
     "does not have to show that anyone played for a stake.",
     "The rules are: 1. Keeping a common gaming house is punishable with imprisonment. 2. The prosecution "
     "does not have to show that anyone played for a stake [1][2]."),
])
def test_a_list_label_is_not_a_sentence_and_never_takes_a_marker(snapshot, answer, expected):
    # The second quote contains "(1)", and its statement shares no word with it.
    check(snapshot, gambling(snapshot)[:1], answer, [PENALTY, NO_PROOF_FULL], expected,
          ["less than one month", "sub-section (1)"])


@pytest.mark.parametrize("answer, expected", [
    # A heading states nothing, however well its one word matches the quote.
    ("Imprisonment:\nA person who keeps a common gaming house may be sent to prison for one month to one year.",
     "Imprisonment:\nA person who keeps a common gaming house may be sent to prison for one month to one year [1]."),
    ("## Imprisonment\nA person who keeps such a place may be sent to jail.",
     "## Imprisonment\nA person who keeps such a place may be sent to jail [1]."),
    ("Penalty:\nA person who keeps such a place may be sent to jail.",
     "Penalty:\nA person who keeps such a place may be sent to jail [1]."),
    # With nothing but a label to attach it to, the source is still shown, and outside the label.
    ("1.", "1. [1]"),
])
def test_a_heading_or_a_bare_label_is_not_a_statement(snapshot, answer, expected):
    check(snapshot, gambling(snapshot)[:1], answer, [PENALTY], expected, ["less than one month"])


@pytest.mark.parametrize("answer, drafts, expected, sections", [
    # Nothing in common with the English quotes: they are placed in the order they were listed.
    (f"{URDU_PUBLIC}۔ {URDU_PROOF}۔", [PUBLIC, NO_PROOF], f"{URDU_PUBLIC} [1]۔ {URDU_PROOF} [2]۔", ["5", "3"]),
    (f"سزائیں:\n۱۔ {URDU_PUBLIC}۔\n۲۔ {URDU_PROOF}۔", [PUBLIC, NO_PROOF],
     f"سزائیں:\n۱۔ {URDU_PUBLIC} [1]۔\n۲۔ {URDU_PROOF} [2]۔", ["5", "3"]),
    (f"{URDU_PUBLIC}۔", [PUBLIC, NO_PROOF], f"{URDU_PUBLIC} [1][2]۔", ["5", "3"]),
    # A number says which quote a statement rests on, in Urdu digits or ours, whatever the listed order.
    (f"{URDU_3_YEARS}۔ {URDU_1_YEAR} یا ۵۰۰ روپے جرمانہ۔", [ONE_YEAR, THREE_YEARS],
     f"{URDU_3_YEARS} [1]۔ {URDU_1_YEAR} یا ۵۰۰ روپے جرمانہ [2]۔", ["5", "4"]),
    ("Aam jagah par jua khelne ki saza 3 saal tak qaid hai. Juay ke adday mein paye jane par 1 saal tak qaid "
     "ya Rs. 500 jurmana hai.", [ONE_YEAR, THREE_YEARS],
     "Aam jagah par jua khelne ki saza 3 saal tak qaid hai [1]. Juay ke adday mein paye jane par 1 saal tak qaid "
     "ya Rs. 500 jurmana hai [2].", ["5", "4"]),
    ("Aam jagah par jua khelne par Rs. 5,000 tak jurmana hai. Adday mein paye jane par Rs. 500 tak jurmana hai.",
     [FINE_500, FINE_5000],
     "Aam jagah par jua khelne par Rs. 5,000 tak jurmana hai [1]. Adday mein paye jane par Rs. 500 tak jurmana "
     "hai [2].", ["5", "4"]),
    # The quote that matches nothing goes to the statement still without a source. The "1" printed
    # against the amendment in "1[three years]" is a footnote number, not a term of one year.
    (f"{URDU_3_YEARS}۔ {URDU_PROOF}۔", [NO_PROOF, THREE_YEARS], f"{URDU_3_YEARS} [1]۔ {URDU_PROOF} [2]۔", ["5", "3"]),
    (f"{URDU_1_YEAR}۔ {URDU_PUBLIC}۔", [THREE_YEARS, ONE_YEAR], f"{URDU_1_YEAR} [1]۔ {URDU_PUBLIC} [2]۔", ["4", "5"]),
])
def test_quotes_without_markers_are_attached_in_any_language(snapshot, answer, drafts, expected, sections):
    grounded = ground_answer(answer, drafts, gambling(snapshot), snapshot)
    assert grounded.answer == expected
    assert [citation.label for citation in grounded.citations] == sections
    assert all(f"[{number}]" in grounded.answer for number in range(1, len(sections) + 1))
    assert (grounded.verified_quotes, grounded.dropped_quotes, grounded.fallback_citations) == (len(drafts), 0, 0)


def test_markers_in_another_language_are_matched_through_numbers(snapshot):
    units = gambling(snapshot)
    urdu = ground_answer(
        "اڈا رکھنے کی سزا ۱ ماہ سے ۱ سال تک قید یا ۱۰۰ سے ۱۰۰۰ روپے جرمانہ ہے [E1]۔ "
        "استغاثہ کو رقم ثابت کرنے کی ضرورت نہیں [E1]۔", [], units, snapshot)
    assert urdu.answer.count("[1]۔") == 1 and urdu.answer.endswith("[2]۔")
    assert "one hundred rupees nor more than one thousand rupees" in urdu.citations[0].quote
    # Nothing in the second statement can be matched: it is cited through the opening words of the section.
    assert urdu.citations[1].quote.startswith("(1) Whoever") and urdu.fallback_citations == 2
    english = ground_answer("The fine is between Rs. 100 and Rs. 1,000 [E1].", [], units, snapshot)
    assert "one hundred rupees nor more than one thousand rupees" in english.citations[0].quote
    # Two quotes of one passage that share no word with the answer are used in the order listed.
    roman = ground_answer(
        "Jua khana chalane ki saza qaid hai [E1]. Yeh sabit karna zaroori nahi ke paisay ke liye khela ja raha "
        "tha [E1].", [PENALTY, NO_PROOF], units, snapshot)
    assert roman.answer.count("[1]") == 1 and roman.answer.endswith("[2].")
    assert [citation.quote for citation in roman.citations] == [PENALTY["quote"], NO_PROOF["quote"]]


@pytest.mark.parametrize("answer, starts, ends", [
    ("A director who connived is punishable with imprisonment up to seven years and a fine up to one million "
     "rupees [E1].", "and every director, manager,", "may extend to one million rupees."),
    ("Punishable with imprisonment up to seven years [E1].", "the business of the company,", "extend to seven years"),
    ("کمپنی کے ڈائریکٹر کو ۷ سال تک قید ہو سکتی ہے [E1]۔", "of the business of the company,", "may extend to seven"),
    # Nothing to go by: the sentence from its start, as far as whole words fit.
    ("کمپنی کے ڈائریکٹر بھی ذمہ دار ہیں [E1]۔", "Where an offence under this Act", "or other officer of the"),
])
def test_a_long_provision_is_quoted_where_it_supports_the_statement(snapshot, answer, starts, ends):
    base = unit(snapshot, "E1", GAMBLING, "5").chunk
    text = f"{base.prefix}\n\n{OFFENCES}"
    span = replace(base.segments[0], start=len(base.prefix) + 2, end=len(text))
    long_unit = EvidenceUnit("E1", replace(base, text=text, heading="Offences by companies.", segments=(span,)),
                             "retrieved")
    grounded = ground_answer(answer, [], [long_unit], snapshot)
    quote = grounded.citations[0].quote
    assert quote.startswith(starts) and quote.endswith(ends) and len(quote) <= 320
    # The quote is a run of whole words of the source.
    position = OFFENCES.index(quote)
    assert OFFENCES[position - 1] == " " and OFFENCES[position + len(quote):][:1] in ("", " ")
    assert grounded.fallback_citations == 1


@pytest.mark.parametrize("answer, drafts, expected, sections", [
    # Control characters cannot pass for the placeholders used while numbering.
    ("Public gaming is punishable \x007\x01 [E2].", [], "Public gaming is punishable 7 [1].", ["5"]),
    ("Forged \x000\x01. Public gaming is punishable [E2].", [], "Forged 0. Public gaming is punishable [1].", ["5"]),
    ("Public gaming \x00 is \x01 punishable [E2].\r\nNothing\x1b else\x08 applies.", [],
     "Public gaming is punishable [1].\nNothing else applies.", ["5"]),
    # A bare number is the marker it was meant to be ...
    ("Gaming in a public place is punishable with three years [2]. Keeping a gaming house is punishable with "
     "imprisonment [1].", [PENALTY, PUBLIC],
     "Gaming in a public place is punishable with three years [1]. Keeping a gaming house is punishable with "
     "imprisonment [2].", ["5", "3"]),
    (f"{PROVING} [2][1].", [], f"{PROVING} [1][2].", ["5", "3"]),
    (f"{PROVING}[E2][1].", [], f"{PROVING}[1][2].", ["5", "3"]),
    (f"{URDU_PROOF} [۱]۔", [], f"{URDU_PROOF} [1]۔", ["3"]),
    # ... and goes like any unknown marker when no passage has that number.
    ("Public gaming is punishable [7].", [PUBLIC], "Public gaming is punishable [1].", ["5"]),
])
def test_control_characters_and_bare_numbers_in_the_answer(snapshot, answer, drafts, expected, sections):
    grounded = ground_answer(answer, drafts, gambling(snapshot), snapshot)
    assert grounded.answer == expected
    assert [citation.label for citation in grounded.citations] == sections


def test_amendment_brackets_of_the_law_are_never_read_as_markers(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "4"), unit(snapshot, "E2", GAMBLING, "5")]
    quoted = 'The term "may extend to 1[three years]" was put into section 3[59] in place of [three years]'
    grounded = ground_answer(f"{quoted} [2].", [], units, snapshot)
    assert grounded.answer == f"{quoted} [1]." and [citation.label for citation in grounded.citations] == ["5"]
    # A number in brackets that touches the text before it is part of what is printed there.
    printed = "Sub-section (2)[3], the words x[2] and the figure 2[5] stay as printed"
    assert ground_answer(f"{printed} [E2].", [], units, snapshot).answer == f"{printed} [1]."


@pytest.mark.parametrize("answer, expected", [
    # A nested list keeps its indentation, and a quoted heading with ".- " is not a list.
    ("Penalties:\n- Public gaming [E2]\n    - up to three years\n\t- or a fine\n"
     "Section 5 reads \"Penalty for gaming in a public place.- Whoever is found gaming\".",
     "Penalties:\n- Public gaming [1]\n    - up to three years\n\t- or a fine\n"
     "Section 5 reads \"Penalty for gaming in a public place.- Whoever is found gaming\"."),
    ("Section 5 reads: \"Penalty for gaming in a public place.- Whoever is found gaming\" [E2].",
     "Section 5 reads: \"Penalty for gaming in a public place.- Whoever is found gaming\" [1]."),
    # Statutes introduce their clauses with ":- (a)".
    ("The Ordinance covers two things, namely:- (a) gaming houses [E1].- (b) public places [E2].",
     "The Ordinance covers two things, namely:- (a) gaming houses [1].- (b) public places [2]."),
    ("Note:  two  spaces [E2] .\n    ...and an indented line  here.",
     "Note: two spaces [1].\n    ...and an indented line here."),
])
def test_only_a_run_on_list_is_broken_into_lines_and_indentation_is_kept(snapshot, answer, expected):
    assert ground_answer(answer, [], gambling(snapshot), snapshot).answer == expected


def test_mixed_markers_and_unmarked_statements_reference_every_quote(snapshot):
    units = [unit(snapshot, "E1", GAMBLING, "5"), unit(snapshot, "E2", CONSUMER, "19")]
    result = ground_answer("Public gaming is punishable [E1]. The seller must issue a receipt.",
                           [{"evidence_id": "E2", "quote": "shall issue to the purchaser a receipt"}],
                           units, snapshot)
    assert len(result.citations) == 2 and "receipt [2]" in result.answer


def test_c1_controls_and_crlf_are_cleaned_without_damaging_lines(snapshot):
    items = [unit(snapshot, "E1", GAMBLING, "5")]
    result = ground_answer("Public gaming.\r\n  - A fine\x85 applies [E1].", [], items, snapshot)
    assert "\x85" not in result.answer and "\n  - A fine" in result.answer
