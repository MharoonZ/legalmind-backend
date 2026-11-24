"""Law-name resolution against published catalogs, including misspelt titles."""
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.retrieval.laws import CatalogLaw, LawResolver

PILOT_TITLES = [
    "The Criminal Law (Special Provisions) Ordinance, 1968",
    "The Good Conduct Prisoners' Probational Release Act, 1926",
    "The Prevention of Corruption Act (West Pakistan Extension) Ordinance, 1958",
    "The Prisoners Act, 1900", "The Prisons Act, 1894", "The Public Gambling (Repeal) Act, 1985",
    "The Punjab Anti-Corruption Establishment Ordinance, 1961", "The Punjab Arms Ordinance, 1965",
    "The Punjab Borstal Act, 1926", "The Punjab Consumer Protection Act 2005",
    "The Punjab Control of Goondas (Removal of Doubts) Ordinance, 1972",
    "The Punjab Control of Goondas Ordinance, 1959",
    "The Punjab Criminal Prosecution Service (Constitution, Functions and Powers) Act 2006",
    "The Punjab Criminal Prosecution Service Inspectorate Act 2018",
    "The Punjab Maintenance of Public Order Ordinance, 1960", "The Punjab Murderous Outrages Act, 1867",
    "The Punjab Prevention of Gambling Ordinance, 1978",
    "The Punjab Private Security Companies (Regulation and Control) Ordinance, 2002",
    "The Punjab Prohibition of Expressing Matters on Walls Act, 1995",
    "The Punjab Suppression of Prostitution Ordinance, 1961", "The Punjab Vagrancy Ordinance, 1958",
    "The Reformatory Schools Act, 1897", "The Restriction of Habitual Offenders (Punjab) Act, 1918",
]


def catalog(titles):
    return LawResolver(CatalogLaw(uuid5(NAMESPACE_URL, title), title) for title in titles)


@pytest.fixture(scope="module")
def pilot():
    return catalog(PILOT_TITLES)


@pytest.mark.parametrize("question,expected", [
    ("What is Section 3 of Punjab Prevention of Gambling Ordinance 1978?", "Prevention of Gambling"),
    # The reported failure: one missing letter in the law's name.
    ("What is Section 3 of Punjb Prevention of Gambling Ordinance 1978?", "Prevention of Gambling"),
    ("What is Section 3 of Punjab Prevenion of Gamblng Ordinence 1978?", "Prevention of Gambling"),
    ("What is Section 3 of the Gambling Ordinance?", "Prevention of Gambling"),
    ("is gambling illegal in punjab under the 1978 ordinance?", "Prevention of Gambling"),
    ("What is S. 3 PPGO 1978?", "Prevention of Gambling"),
    ("section 3 gambling law punjab", "Prevention of Gambling"),
    ("What is Section 3 of the Prison Act 1894?", "Prisons Act"),
    ("section 5 of the prisons act", "Prisons Act"),
    ("What is Section 19 of the Consumer Protecton Act 2005?", "Consumer Protection"),
    ("what is section 3 of the consumer act", "Consumer Protection"),
    ("CPA 2005 section 25", "Consumer Protection"),
    ("Tell me the penalty under the Arms Ordinance for carrying unlicensed weapons", "Arms"),
    ("section 4 of the criminal prosecution service act 2006", "Constitution, Functions"),
    ("goondas ordinance section 2", "Control of Goondas Ordinance, 1959"),
    ("section 3 of the anti corruption ordinance", "Anti-Corruption"),
    ("prevention of corruption act section 2", "Prevention of Corruption"),
    ("habitual offenders act", "Habitual Offenders"),
    ("reformatory school act section 8", "Reformatory Schools"),
    ("murderous outrages act 1867", "Murderous Outrages"),
    ("MPO section 16", "Maintenance of Public Order"),
    ("public order ordinance", "Maintenance of Public Order"),
    ("Is this covered? The Arms Ordinance says something about licences.", "Arms"),
])
def test_named_laws_resolve_despite_typos_abbreviations_and_partial_titles(pilot, question, expected):
    result = pilot.resolve(question, loose="section" in question.lower() or "S." in question)
    assert result.status == "resolved", (result.status, [m.title for m in result.matches])
    assert expected in result.matches[0].title


@pytest.mark.parametrize("question,expected", [
    ("section 3 of the gambling act", {"Public Gambling (Repeal)", "Prevention of Gambling"}),
    ("section 5 of the prison act", {"Prisons Act", "Prisoners Act"}),
    ("section 4 of the prosecution service act", {"Inspectorate", "Constitution, Functions"}),
])
def test_equally_plausible_laws_are_reported_as_ambiguous(pilot, question, expected):
    result = pilot.resolve(question)
    assert result.status == "ambiguous"
    assert all(any(fragment in match.title for match in result.matches) for fragment in expected)


@pytest.mark.parametrize("question,mention", [
    ("What is the punishment for murder under the Pakistan Penal Code?", "Pakistan Penal Code"),
    ("section 12 of the Contract Act 1872", "Contract Act 1872"),
    ("what does the Transfer of Property Act say", "Transfer of Property Act"),
    ("Under the Qanun-e-Shahadat Order 1984, is hearsay admissible?", "Qanun-e-Shahadat Order 1984"),
    ("as per the Code of Criminal Procedure section 144", "Code of Criminal Procedure"),
    ("the motor vehicle ordinance 1965 section 3", "motor vehicle ordinance 1965"),
])
def test_a_named_law_outside_the_collection_is_unresolved_not_guessed(pilot, question, mention):
    result = pilot.resolve(question)
    assert result.status == "unresolved" and result.mention == mention and not result.matches


@pytest.mark.parametrize("question", [
    "what is the punishment for gambling in a public place?",
    "what does the law say about prisoners release on good conduct",
    "a criminal act committed by a prisoner",
    "under which act is gambling punished",
    "an act of gambling in public",
    "the code of conduct for prisoners",
    "in order to gamble",
    "The shopkeeper refused to give me a receipt. Must a seller provide a bill?",
    "What is Section 3?",
])
def test_topic_words_and_ordinary_english_do_not_name_a_law(pilot, question):
    assert pilot.resolve(question).status == "none"


def test_loose_mode_needs_real_title_coverage(pilot):
    assert pilot.resolve("what is section 3 punjab", loose=True).status == "none"
    assert pilot.resolve("section 3 gambling", loose=True).status in {"resolved", "ambiguous"}


def test_excluded_spans_never_become_part_of_a_law_name(pilot):
    question = "section 1978 of the arms ordinance"
    assert "Arms" in pilot.resolve(question, exclude=[(0, 12)]).matches[0].title


def test_resolve_name_treats_its_input_as_a_law_name(pilot):
    assert "Vagrancy" in pilot.resolve_name("vagrancy").matches[0].title
    assert "Borstal" in pilot.resolve_name("Punjab Borstal Act 1926").matches[0].title
    unknown = pilot.resolve_name("Income Tax Ordinance 2001")
    assert unknown.status == "unresolved" and unknown.mention == "Income Tax Ordinance 2001"


def test_aliases_and_unseen_catalogs_work_without_code_changes():
    resolver = LawResolver([
        CatalogLaw(uuid5(NAMESPACE_URL, "ita"), "Income Tax Ordinance, 2001", ("ITO 2001",)),
        CatalogLaw(uuid5(NAMESPACE_URL, "sta"), "Sales Tax Act, 1990"),
        CatalogLaw(uuid5(NAMESPACE_URL, "ca"), "Companies Act, 2017"),
    ])
    assert resolver.resolve("sec 114 of the Incom Tax Ordinnce").matches[0].title == "Income Tax Ordinance, 2001"
    assert resolver.resolve("the sales tax act").matches[0].title == "Sales Tax Act, 1990"
    assert resolver.resolve("compnies act 2017 section 2").matches[0].title == "Companies Act, 2017"
    assert resolver.resolve("the 1990 act").matches[0].title == "Sales Tax Act, 1990"
    assert resolver.resolve("Customs Act 1969 section 2").status == "unresolved"
    assert {law.title for law in resolver.laws} >= {"Companies Act, 2017"}


def test_empty_input_and_empty_catalog_are_harmless():
    assert catalog([]).resolve("section 3 of the gambling ordinance").status in {"none", "unresolved"}
    assert catalog(PILOT_TITLES).resolve("   ").status == "none"


@pytest.mark.parametrize("phrase", ["Northern Consumer Protection Act", "Coastal Prisons Act",
                                   "northern consumer protection act"])
def test_distinctive_modifier_of_an_unpublished_title_is_preserved(pilot, phrase):
    for resolved in (pilot.resolve("Under the " + phrase + ", what are the rules?"), pilot.resolve_name(phrase)):
        assert resolved.status == "unresolved" and phrase in resolved.mention
    assert pilot.resolve_name("coastal prisons").status == "unresolved"


def test_long_ordinary_questions_scan_for_an_acronyms_year_once(pilot, monkeypatch):
    import app.retrieval.laws as laws
    calls = []
    original = laws._is_year

    def counted(word):
        calls.append(word)
        return original(word)

    monkeypatch.setattr(laws, "_is_year", counted)
    question = ("Please explain how members obtain permits. " * 96)[:4000]
    assert pilot.resolve(question).status == "none"
    assert len(calls) < len(question.split()) * 5
