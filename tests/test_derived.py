"""Derived-value corroboration tests.

Case 1 asks for a fact corroborated across sources "even if expressed differently". The
hardest version of that is when no two facts are the same claim at all, but the arithmetic
between them holds. These tests also pin down the guard that stops it becoming numerology.
"""

from crosscheck.reason.derived import find_all, find_growth, find_ratios

from .factories import mk


def test_margin_is_corroborated_by_its_numerator_and_denominator():
    """The FY24 deck states EBITDA, revenue and an EBITDA margin. None of the three is the
    same claim as another, yet the margin is exactly what the other two imply."""
    facts = [
        mk("ebitda", "₹127 Cr", period="FY24"),
        mk("revenue from services", "₹8,142 Cr", period="FY24"),
        mk("ebitda margin", "1.6%", period="FY24"),
    ]
    found = find_ratios(facts)
    assert len(found) == 1
    d = found[0]
    assert d.target.attribute == "ebitda margin"
    assert {p.attribute for p in d.parts} == {"ebitda", "revenue from services"}
    assert abs(d.computed - 1.56) < 0.01
    assert "matching the stated" in d.explanation


def test_unrelated_attributes_are_not_linked_by_coincidence():
    """Among many figures sharing a period, some pair divides into some percentage by
    chance. Requiring the names to overlap is what makes the arithmetic mean something."""
    facts = [
        mk("pin-code reach", "18,793", period="FY24"),
        mk("active customers", "33,278", period="FY24"),
        mk("gross margin", "56.5%", period="FY24"),
    ]
    assert find_ratios(facts) == []


def test_ratio_requires_matching_unit_families():
    """A currency amount over a headcount is not a margin."""
    facts = [
        mk("ebitda", "₹127 Cr", period="FY24"),
        mk("ebitda headcount", "8142", period="FY24"),
        mk("ebitda margin", "1.6%", period="FY24"),
    ]
    assert find_ratios(facts) == []


def test_a_wrong_margin_is_not_corroborated():
    facts = [
        mk("ebitda", "₹127 Cr", period="FY24"),
        mk("revenue from services", "₹8,142 Cr", period="FY24"),
        mk("ebitda margin", "9.4%", period="FY24"),
    ]
    assert find_ratios(facts) == []


def test_year_on_year_growth_is_corroborated_by_the_two_levels():
    """FY23 to FY24 revenue implies the stated 12.7% growth."""
    facts = [
        mk("revenue from services", "₹7,225 Cr", period="FY23"),
        mk("revenue from services", "₹8,142 Cr", period="FY24"),
        mk("revenue from services growth", "12.7%", period="FY24"),
    ]
    found = find_growth(facts)
    assert len(found) == 1
    assert abs(found[0].computed - 12.69) < 0.1
    assert found[0].kind == "growth"


def test_growth_ignores_non_adjacent_periods():
    """FY22 to FY24 is not a year-on-year change, whatever the arithmetic says."""
    facts = [
        mk("revenue from services", "₹7,225 Cr", period="FY22"),
        mk("revenue from services", "₹8,142 Cr", period="FY24"),
        mk("revenue from services growth", "12.7%", period="FY24"),
    ]
    assert find_growth(facts) == []


def test_facts_without_a_period_are_skipped():
    facts = [
        mk("ebitda", "₹127 Cr"),
        mk("revenue from services", "₹8,142 Cr"),
        mk("ebitda margin", "1.6%"),
    ]
    assert find_ratios(facts) == []


def test_derivations_work_across_documents():
    """The deck states the margin; the annual report states the components."""
    facts = [
        mk("ebitda", "₹127 Cr", period="FY24", doc_id=2),
        mk("revenue from services", "₹8,142 Cr", period="FY24", doc_id=2),
        mk("ebitda margin", "1.6%", period="FY24", doc_id=3),
    ]
    found = find_all(facts)
    assert found and found[0].target.doc_id == 3
    assert {p.doc_id for p in found[0].parts} == {2}


def test_a_row_dump_without_punctuation_is_not_used_as_a_denominator():
    """Regression for a real false corroboration found on the live corpus: a table row
    copied as "216.68 16.24 16.33 9.75 9.58" -- no '=' or ';', so it slips past
    extraction's own row-dump guard -- was used as a denominator, and parse_value's
    first-number-only reading (216.68) happened to divide into something close enough to
    an unrelated target to look like a match. No legitimate single value contains more
    than one number."""
    facts = [
        mk("share capital breakdown", "216.68 16.24 16.33 9.75 9.58", period="FY24"),
        mk("post-offer paid up capital", "14.84%", period="FY24"),
        mk("some other level", "1,450.02", period="FY24"),
    ]
    assert find_ratios(facts) == []


def test_a_generic_attribute_label_is_never_used_as_a_denominator():
    """Regression for a real false corroboration: 'rights plan expense' divided by a
    fact labelled only 'other line' landed within tolerance of a stated volatility
    percentage -- purely because 'other line' is free to pair with anything, since it
    names nothing. _related() alone does not catch this: the genuine EBITDA-margin case
    has no word overlap between 'ebitda' and 'revenue from services' either, so the guard
    has to target vagueness specifically, not tighten word-overlap generally."""
    facts = [
        mk("rights plan expense", "40.99", period="FY24"),
        mk("other line", "94.13", period="FY24"),
        mk("expected volatility", "43.48%", period="FY24"),
    ]
    assert find_ratios(facts) == []


def test_generic_labels_do_not_break_the_genuine_ebitda_case():
    """The guard must be specific to vague labels, not so broad it damages the case it
    exists to protect."""
    facts = [
        mk("ebitda", "₹127 Cr", period="FY24"),
        mk("revenue from services", "₹8,142 Cr", period="FY24"),
        mk("ebitda margin", "1.6%", period="FY24"),
    ]
    assert find_ratios(facts)
