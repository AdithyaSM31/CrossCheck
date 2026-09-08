import pytest

from crosscheck.normalize.values import (
    ParsedValue,
    ValueKind,
    comparable,
    parse_value,
    values_agree,
)


@pytest.mark.parametrize(
    "raw,kind,number,unit",
    [
        # Strings taken from the starter corpus.
        ("₹8,142 Cr", ValueKind.MONEY, 8.142e10, "INR"),
        ("₹127Cr", ValueKind.MONEY, 1.27e9, "INR"),
        ("₹2,076 Cr", ValueKind.MONEY, 2.076e10, "INR"),
        ("₹40,000.00 million", ValueKind.MONEY, 4.0e10, "INR"),
        ("Rs. 578 Cr", ValueKind.MONEY, 5.78e9, "INR"),
        ("US$ 1.2 billion", ValueKind.MONEY, 1.2e9, "USD"),
        ("₹1.5 lakh crore", ValueKind.MONEY, 1.5e12, "INR"),
        ("6.5 per cent", ValueKind.PERCENT, 6.5, "%"),
        ("1.6%", ValueKind.PERCENT, 1.6, "%"),
        ("73 bps", ValueKind.POINTS, 0.73, "pp"),
        ("18,074", ValueKind.NUMBER, 18074, None),
        ("1.4 Mn Tons", ValueKind.QUANTITY, 1.4e6, "tons"),
        ("740 Mn", ValueKind.NUMBER, 7.4e8, None),
        ("31 days", ValueKind.QUANTITY, 31, "days"),
    ],
)
def test_parse(raw, kind, number, unit):
    v = parse_value(raw)
    assert v.kind is kind
    assert v.number == pytest.approx(number)
    assert v.unit == unit


@pytest.mark.parametrize(
    "raw,number",
    [
        ("₹(452) Cr", -4.52e9),
        ("Rs. (452 Cr)", -4.52e9),
        ("(6.3%)", -6.3),
        ("(1,008)", -1008),
        ("-2.2%", -2.2),
    ],
)
def test_parenthesised_values_are_negative(raw, number):
    """Financial reporting writes losses in parentheses; reading them positive would
    turn a loss into a profit and invent contradictions."""
    assert parse_value(raw).number == pytest.approx(number)


def test_crore_and_million_reconcile():
    """The prospectus quotes ₹ million where the earnings deck quotes ₹ crore. Same corpus,
    same currency, different scale word — these must land on the same number."""
    a = parse_value("₹40,000.00 million")
    b = parse_value("₹4,000 Cr")
    assert values_agree(a, b)[0]


def test_rounded_restatement_corroborates():
    ok, why = values_agree(parse_value("₹8,142 Cr"), parse_value("₹8,100 Cr"))
    assert ok and "significant figures" in why


def test_rounding_leniency_is_floored_at_two_sig_figs():
    """Without the floor, '₹1,000 Cr' would be one significant figure and would agree
    with almost any number of the same magnitude."""
    assert not values_agree(parse_value("₹1,000 Cr"), parse_value("₹1,400 Cr"))[0]


def test_close_but_distinct_percentages_disagree():
    assert not values_agree(parse_value("6.5 per cent"), parse_value("6.9 per cent"))[0]


def test_precise_figures_are_not_rounded_together():
    assert not values_agree(parse_value("18,074"), parse_value("18,540"))[0]


def test_currencies_are_not_silently_compared():
    assert not comparable(parse_value("₹100 Cr"), parse_value("US$ 100 million"))


def test_percent_and_points_are_different_things():
    """A level of 4.6% and a change of 4.6pp are not the same claim."""
    assert not comparable(parse_value("4.6%"), parse_value("4.6 percentage points"))


def test_bare_number_compares_against_a_percent():
    """Table cells routinely lose the unit their column header carried."""
    assert comparable(parse_value("9.2"), parse_value("9.2 per cent"))


def test_unknown_unit_is_preserved_rather_than_dropped():
    v = parse_value("2.8 Bn shipments")
    assert v.number == pytest.approx(2.8e9)
    assert v.unit == "shipments"


def test_unparseable_text_degrades_to_text():
    v = parse_value("not applicable")
    assert v.kind is ValueKind.TEXT and v.number is None


def test_incomparable_values_never_agree():
    a, b = parse_value("₹100 Cr"), parse_value("not applicable")
    assert not values_agree(a, b)[0]
