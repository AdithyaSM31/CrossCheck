"""Grounding gate tests.

The gate decides what is allowed to become a fact. Its job is not to be lenient — a
hallucinated fact that passes looks identical to a real one for the rest of the system's
life, while a rejected real fact is visible in the review queue and can be recovered.
"""

import pytest

from crosscheck.ground.verify import ground, normalise, value_in_span, verify_quote

BLOCK = (
    "[Section: FY24 highlights]\n"
    "FY24 EBITDA increased by Rs. 578 Cr to Rs. 127 Cr from Rs. (452 Cr) in FY23\n"
    "Real GDP (at market prices): 2021/22=9.7; 2022/23=7.6; 2023/24=9.2; 2024/25=6.5\n"
    "[Footnote on same page: (2) Growth rate of revenue from services "
    "(excluding revenue from traded goods)]"
)


def test_exact_quote_is_verbatim():
    g = verify_quote("FY24 EBITDA increased by Rs. 578 Cr to Rs. 127 Cr", BLOCK)
    assert g.status == "verbatim" and g.score == 100.0


def test_whitespace_and_unicode_drift_is_forgiven():
    """Curly quotes, non-breaking spaces and collapsed newlines are never meaningful."""
    g = verify_quote("FY24  EBITDA increased by Rs. 578 Cr\nto Rs. 127 Cr", BLOCK)
    assert g.ok


def test_paraphrase_is_rejected():
    """A quote that reads plausibly but was composed rather than copied."""
    g = verify_quote("EBITDA for the year rose by 578 crore to reach 127 crore", BLOCK)
    assert g.status == "failed"


def test_invented_quote_is_rejected():
    g = verify_quote("The company reported a record profit of Rs. 900 Cr in FY24", BLOCK)
    assert g.status == "failed"


def test_minor_typo_still_grounds_fuzzily():
    g = verify_quote("FY24 EBITDA increased by Rs. 578 Cr to Rs. 127 Cr from Rs (452 Cr)", BLOCK)
    assert g.ok


def test_value_must_be_inside_the_quote():
    """The dangerous case: a real quote paired with a number taken from a neighbouring row.
    The quote verifies perfectly, so only this check catches it."""
    quote = "Real GDP (at market prices): 2021/22=9.7; 2022/23=7.6; 2023/24=9.2; 2024/25=6.5"
    g, why = ground(quote, "8.2", BLOCK)
    assert not g.ok and "not present" in why


def test_value_present_in_quote_passes():
    quote = "Real GDP (at market prices): 2021/22=9.7; 2022/23=7.6; 2023/24=9.2; 2024/25=6.5"
    g, why = ground(quote, "9.2", BLOCK)
    assert g.ok and why == ""


def test_value_matches_across_formatting():
    """The cell reads '2023/24=9.2'; the model reports the value as '9.2 per cent'."""
    ok, _ = value_in_span("9.2 per cent", "real gdp (at market prices): 2023/24=9.2")
    assert ok


def test_currency_formatting_does_not_block_the_match():
    ok, _ = value_in_span("Rs. 127 Cr", "fy24 ebitda increased by rs. 578 cr to rs. 127 cr")
    assert ok


def test_non_numeric_value_matches_on_words():
    ok, _ = value_in_span("resigned", "mr. sharma resigned from the board with effect from")
    assert ok


def test_empty_and_tiny_quotes_are_rejected():
    assert verify_quote("", BLOCK).status == "failed"
    assert verify_quote("Rs. 9,999", BLOCK).status == "failed"


def test_normalise_folds_dashes_quotes_and_ligatures():
    assert normalise("“con‐firmed”") == '"con-firmed"'
    assert normalise("A  B\nC") == "a b c"


@pytest.mark.parametrize(
    "value,span,expected",
    [
        ("18,074", "pin-code reach(1): q4 fy22=18,074", True),
        ("18,074", "pin-code reach(1): q4 fy22=18,540", False),
        ("(6.3%)", "fy23: rs.(452) cr / (6.3%)", True),
        ("(5.6%)", "fy23: rs.(452) cr / (6.3%)", False),
    ],
)
def test_digit_matching_is_strict_about_which_number(value, span, expected):
    assert value_in_span(value, span)[0] is expected
