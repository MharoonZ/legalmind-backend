"""Text normalisation and the typo-tolerant BM25 index."""
import numpy as np
import pytest

from app.retrieval.lexical import LexicalIndex
from app.retrieval.text import compact_label, edit_budget, fold, normalize, stem, typo_distance, words


def test_normalize_keeps_wording_and_folds_only_for_matching():
    assert normalize("  Section １９\n of  the Act ") == "Section 19 of the Act"
    assert fold("Qānūn-e-Shahādat") == "qanun-e-shahadat"
    assert words("Prisoners' Act, 1900") == ["prisoners", "act", "1900"]


def test_stemming_leaves_identifiers_and_short_words_alone():
    assert stem("prisoners") == stem("prison") == "prison"
    assert stem("5a") == "5a" and stem("act") == "act" and stem("1978") == "1978"


@pytest.mark.parametrize("left,right,expected", [
    ("punjb", "punjab", 1), ("sectoin", "section", 1), ("prevenion", "prevention", 1),
    ("gamblng", "gambling", 1), ("act", "art", None), ("prisoners", "prisons", None),
    ("ordinence", "ordinance", 1), ("xyz", "xyz", 0),
])
def test_typo_distance_scales_with_word_length(left, right, expected):
    assert typo_distance(left, right) == expected


def test_edit_budget_never_guesses_short_words():
    assert [edit_budget(n) for n in (1, 3, 4, 7, 8, 20)] == [0, 0, 1, 1, 2, 2]


@pytest.mark.parametrize("label,expected", [("5-A", "5a"), ("5 A", "5a"), ("14A", "14a"), (None, ""), ("19", "19")])
def test_printed_labels_compare_without_hyphens_or_case(label, expected):
    assert compact_label(label) == expected


DOCUMENTS = [
    [("Gambling Ordinance", 1.0), ("Penalty for gaming in a public place", 2.5),
     ("Whoever is found gaming in any public place shall be punishable with imprisonment", 1.0)],
    [("Gambling Ordinance", 1.0), ("Power to enter and search", 2.5),
     ("A Magistrate may authorise a police officer to enter and search a gaming house", 1.0)],
    [("Consumer Protection Act", 1.0), ("Receipt to be issued to the purchaser", 2.5),
     ("Every trader who sells goods shall issue a receipt to the purchaser", 1.0)],
]


def test_bm25_ranks_the_matching_document_first():
    index = LexicalIndex(DOCUMENTS)
    terms, repairs = index.query_terms("must a trader issue a receipt")
    assert not repairs
    assert index.search(terms)[0][0] == 2


def test_misspelt_words_are_repaired_from_the_corpus_vocabulary():
    index = LexicalIndex(DOCUMENTS)
    terms, repairs = index.query_terms("punishmnt for gamng in publc place")
    assert repairs == {"gamng": "gaming", "publc": "public"}
    assert index.search(terms)[0][0] == 0
    assert index.repair("zzzzzzzz") == [] and index.repair("the") == []


def test_repaired_terms_weigh_less_than_exact_terms():
    index = LexicalIndex(DOCUMENTS)
    exact = dict(index.query_terms("receipt")[0])
    repaired = dict(index.query_terms("reciept")[0])
    assert exact[stem("receipt")] == 1.0 and 0 < repaired[stem("receipt")] < 1.0


def test_mask_limits_results_and_unknown_queries_return_nothing():
    index = LexicalIndex(DOCUMENTS)
    terms, _ = index.query_terms("gaming")
    assert {doc for doc, _ in index.search(terms)} == {0, 1}
    assert [doc for doc, _ in index.search(terms, mask=np.array([False, True, True]))] == [1]
    assert index.search(index.query_terms("quantum entanglement")[0]) == []
    assert index.search([]) == [] and LexicalIndex([]).search([("x", 1.0)]) == []


def test_heading_matches_outrank_body_matches():
    index = LexicalIndex([
        [("Act", 1.0), ("Search", 2.5), ("General provisions about other matters entirely", 1.0)],
        [("Act", 1.0), ("Other matters", 2.5), ("A search may be made of general provisions entirely", 1.0)],
    ])
    assert index.search(index.query_terms("search")[0])[0][0] == 0
