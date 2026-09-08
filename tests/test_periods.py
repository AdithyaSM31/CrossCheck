from datetime import date

import pytest

from crosscheck.normalize.periods import (
    Granularity,
    PeriodRelation,
    find_periods,
    parse_period,
    relation,
    same_period,
)


@pytest.mark.parametrize(
    "raw,start,end,gran",
    [
        ("FY24", date(2023, 4, 1), date(2024, 3, 31), Granularity.YEAR),
        ("FY 2023-24", date(2023, 4, 1), date(2024, 3, 31), Granularity.YEAR),
        ("2023-24", date(2023, 4, 1), date(2024, 3, 31), Granularity.YEAR),
        ("2024-25", date(2024, 4, 1), date(2025, 3, 31), Granularity.YEAR),
        ("FY2024/25", date(2024, 4, 1), date(2025, 3, 31), Granularity.YEAR),
        ("2021/22", date(2021, 4, 1), date(2022, 3, 31), Granularity.YEAR),
        ("Q4 FY24", date(2024, 1, 1), date(2024, 3, 31), Granularity.QUARTER),
        ("Q1 FY24", date(2023, 4, 1), date(2023, 6, 30), Granularity.QUARTER),
        ("Q3 FY24", date(2023, 10, 1), date(2023, 12, 31), Granularity.QUARTER),
        ("H1 FY24", date(2023, 4, 1), date(2023, 9, 30), Granularity.HALF),
        ("9M FY24", date(2023, 4, 1), date(2023, 12, 31), Granularity.NINE_MONTH),
        ("CY2024", date(2024, 1, 1), date(2024, 12, 31), Granularity.YEAR),
        ("March 2024", date(2024, 3, 1), date(2024, 3, 31), Granularity.MONTH),
        ("as of March 31, 2024", date(2024, 3, 31), date(2024, 3, 31), Granularity.INSTANT),
        ("as at 31 March 2024", date(2024, 3, 31), date(2024, 3, 31), Granularity.INSTANT),
    ],
)
def test_parse(raw, start, end, gran):
    p = parse_period(raw)
    assert p is not None, raw
    assert (p.start, p.end, p.granularity) == (start, end, gran)


def test_three_publishers_naming_the_same_twelve_months():
    """The core cross-document requirement. The Economic Survey writes FY25, the RBI writes
    2024-25 and the IMF writes FY2024/25 — all one interval. Comparing labels instead of
    intervals would make three agreeing sources look like three different claims."""
    survey = parse_period("FY25")
    rbi = parse_period("2024-25")
    imf = parse_period("FY2024/25")
    assert same_period(survey, rbi)
    assert same_period(rbi, imf)
    assert survey.key() == imf.key() == "2024-04-01/2025-03-31"


def test_labels_are_canonical_across_spellings():
    assert parse_period("FY24").label == parse_period("2023-24").label == "FY2023-24"


def test_annual_contains_its_fourth_quarter():
    """₹8,142 Cr (FY24) vs ₹2,076 Cr (Q4 FY24) is containment, not contradiction — this is
    what lets the reconciler explain the apparent conflict by period."""
    assert relation(parse_period("FY24"), parse_period("Q4 FY24")) is PeriodRelation.CONTAINS
    assert relation(parse_period("Q4 FY24"), parse_period("FY24")) is PeriodRelation.CONTAINED_BY


def test_consecutive_years_are_disjoint():
    assert relation(parse_period("FY23"), parse_period("FY24")) is PeriodRelation.DISJOINT


def test_fiscal_and_calendar_years_overlap_without_being_equal():
    r = relation(parse_period("FY24"), parse_period("CY2023"))
    assert r is PeriodRelation.OVERLAPS


def test_four_digit_fy_is_flagged_ambiguous():
    """FY2024 means the year ending March 2024 in India but the year beginning 2024 to some
    publishers. We pick a convention and record that we did."""
    p = parse_period("FY2024")
    assert p.ambiguous and p.notes


def test_bare_year_is_flagged_ambiguous():
    p = parse_period("2024")
    assert p.ambiguous and p.granularity is Granularity.YEAR


def test_multi_year_range_is_not_a_fiscal_year():
    """'2019-2023' is a span of calendar years, not FY2023."""
    assert parse_period("2019-2023") is None


@pytest.mark.parametrize("text", ["page 12-14", "note 12-14", "12-14 per cent", "Rs. 10-12 crore"])
def test_bare_two_digit_ranges_are_not_fiscal_years(text):
    """Regression: a bare pair used to swallow page ranges and numeric ranges, so
    'page 12-14' parsed as FY2013-14 and attached a confident period to an unrelated fact.
    A pair is only fiscal when FY-prefixed or led by a four-digit year."""
    assert parse_period(text) is None


def test_non_period_text_returns_none():
    assert parse_period("consolidated") is None
    assert parse_period("") is None


def test_find_periods_in_a_sentence():
    text = "FY24 EBITDA increased by Rs. 578 Cr to Rs. 127 Cr from Rs. (452 Cr) in FY23"
    keys = {p.label for p in find_periods(text)}
    assert {"FY2023-24", "FY2022-23"} <= keys
