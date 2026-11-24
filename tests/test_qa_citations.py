"""Locating quotes in the published text and numbering citations."""
import pytest

from app.qa.citations import ground_answer, locate_quote
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
