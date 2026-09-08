"""Claim key tests.

The claim key is the join the whole system turns on. These tests pin down the two
behaviours that matter: facts that are genuinely about the same thing must collide, and
facts that only look alike must not.
"""

from crosscheck.normalize.periods import parse_period
from crosscheck.reason.keys import claim_key, differing_components, loose_key, subject_key


def key(subject, attribute, period=None, **kw):
    return claim_key(
        subject=subject,
        attribute=attribute,
        period=parse_period(period) if period else None,
        **kw,
    )


def test_same_claim_across_publishers_collides():
    """The Survey's FY25, the RBI's 2024-25 and the IMF's FY2024/25 are one claim. If these
    produced three different keys, three agreeing sources would never be compared."""
    a = key("India", "real GDP growth", "FY25", unit_family="percent")
    b = key("India", "real GDP growth", "2024-25", unit_family="percent")
    c = key("India", "real GDP growth", "FY2024/25", unit_family="percent")
    assert a == b == c


def test_wording_differences_in_the_attribute_collide():
    assert key("India", "Real GDP Growth") == key("India", "the real GDP growth")


def test_company_suffixes_are_ignored_in_the_subject():
    assert subject_key("Delhivery Limited") == subject_key("Delhivery Ltd.")
    assert subject_key("Delhivery") == subject_key("Delhivery Limited")


def test_different_periods_do_not_collide():
    """The whole point of case 3: an annual and a quarterly figure are different claims."""
    annual = key("Delhivery", "revenue from services", "FY24", unit_family="currency:INR")
    q4 = key("Delhivery", "revenue from services", "Q4 FY24", unit_family="currency:INR")
    assert annual != q4
    assert differing_components(annual, q4) == ["period"]


def test_different_basis_does_not_collide_and_is_named():
    a = key("Delhivery", "revenue", "FY24", basis="excluding traded goods")
    b = key("Delhivery", "revenue", "FY24", basis=None)
    assert differing_components(a, b) == ["basis"]


def test_different_scope_is_named():
    a = key("Delhivery", "revenue", "FY24", scope="consolidated")
    b = key("Delhivery", "revenue", "FY24", scope="standalone")
    assert differing_components(a, b) == ["scope"]


def test_different_units_are_not_the_same_claim():
    a = key("India", "current account deficit", "FY24", unit_family="percent")
    b = key("India", "current account deficit", "FY24", unit_family="currency:USD")
    assert differing_components(a, b) == ["unit"]


def test_multiple_differing_components_are_all_reported():
    a = key("Delhivery", "revenue", "FY24", scope="consolidated")
    b = key("Delhivery", "revenue", "Q4 FY24", scope="standalone")
    assert differing_components(a, b) == ["period", "scope"]


def test_loose_key_ignores_qualifiers():
    """The candidate key: facts worth comparing, whether or not their qualifiers agree.
    This is the population where apparent contradictions get explained."""
    a = loose_key("Delhivery Limited", "revenue from services")
    b = loose_key("Delhivery", "the revenue from services")
    assert a == b


def test_loose_key_still_separates_different_measures():
    assert loose_key("India", "real GDP growth") != loose_key("India", "CPI inflation")
