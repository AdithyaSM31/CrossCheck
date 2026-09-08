"""Reconciliation rule tests.

These encode the four cases the assignment asks for, at the level where the decision is
actually made. The values are the real ones from the starter corpus.
"""

from crosscheck.reason import rules

from .factories import mk


# --------------------------------------------------------------- case 1: corroboration
def test_identical_claim_and_value_corroborates():
    a = mk("revenue from services", "₹8,142 Cr", period="FY24")
    b = mk("revenue from services", "₹8,142 Cr", period="FY2023-24", doc_id=2)
    v = rules.classify(a, b)
    assert v.label == rules.CORROBORATES


def test_corroboration_survives_different_scale_words():
    """The prospectus writes ₹ million where the deck writes ₹ crore."""
    a = mk("net proceeds", "₹40,000.00 million", period="FY22")
    b = mk("net proceeds", "₹4,000 Cr", period="FY2021-22", doc_id=2)
    assert rules.classify(a, b).label == rules.CORROBORATES


def test_corroboration_survives_rounding():
    a = mk("revenue from services", "₹8,142 Cr", period="FY24")
    b = mk("revenue from services", "₹8,100 Cr", period="FY24", doc_id=2)
    assert rules.classify(a, b).label == rules.CORROBORATES


def test_three_publishers_naming_a_period_differently_still_corroborate():
    survey = mk("real gdp growth", "6.5 per cent", subject="India", period="FY25", doc_id=4)
    rbi = mk("real gdp growth", "6.5 per cent", subject="India", period="2024-25", doc_id=5)
    imf = mk("real gdp growth", "6.5", subject="India", period="FY2024/25", doc_id=6)
    assert rules.classify(survey, rbi).label == rules.CORROBORATES
    assert rules.classify(rbi, imf).label == rules.CORROBORATES


# --------------------------------------------------------------- case 2: contradiction
def test_same_claim_with_conflicting_values_goes_to_the_model():
    """Rules deliberately stop here. Whether this is a contradiction or a later vintage
    revising an earlier estimate depends on reading what each source says about firmness."""
    a = mk("real gdp growth", "9.2", subject="India", period="2023-24", doc_id=6)
    b = mk("real gdp growth", "8.2", subject="India", period="2023-24", doc_id=5)
    v = rules.classify(a, b)
    assert v.label is None and v.needs_llm
    assert "values differ" in v.detail


def test_conflicting_non_numeric_statements_go_to_the_model():
    a = mk("directorship status", "active", subject="Sahil Barua", period=None)
    b = mk("directorship status", "resigned", subject="Sahil Barua", period=None, doc_id=2)
    v = rules.classify(a, b)
    assert v.needs_llm


# ------------------------------------------------- case 3: apparent conflict, explained
def test_annual_versus_quarterly_is_explained_by_period():
    """₹8,142 Cr and ₹2,076 Cr look like a flat contradiction until you notice one is the
    year and the other its fourth quarter."""
    year = mk("revenue from services", "₹8,142 Cr", period="FY24")
    q4 = mk("revenue from services", "₹2,076 Cr", period="Q4 FY24")
    v = rules.classify(year, q4)
    assert v.label == rules.RECONCILED
    assert v.discriminator.startswith("period")
    assert "part of the other" in v.discriminator


def test_differing_basis_is_named_as_the_explanation():
    a = mk("revenue", "₹8,142 Cr", period="FY24", basis="excluding revenue from traded goods")
    b = mk("revenue", "₹8,932 Cr", period="FY24", doc_id=2)
    v = rules.classify(a, b)
    assert v.label == rules.RECONCILED and v.discriminator == "basis"


def test_differing_scope_is_named_as_the_explanation():
    a = mk("revenue", "₹8,142 Cr", period="FY24", scope="consolidated")
    b = mk("revenue", "₹7,900 Cr", period="FY24", scope="standalone", doc_id=2)
    v = rules.classify(a, b)
    assert v.label == rules.RECONCILED and v.discriminator == "scope"


def test_differing_unit_is_named_as_the_explanation():
    """Current account deficit as a share of GDP against the same deficit in dollars."""
    a = mk("current account deficit", "0.6 per cent", subject="India", period="FY25")
    b = mk("current account deficit", "US$ 23.3 billion", subject="India", period="FY25",
           doc_id=6)
    v = rules.classify(a, b)
    assert v.label == rules.RECONCILED and v.discriminator == "unit"


# --------------------------------------------------------------- restraint
def test_different_periods_with_agreeing_values_says_nothing():
    """Two different periods that happen to share a value is not a finding. Reporting it
    would bury the real cases in noise."""
    a = mk("revenue from services", "₹8,142 Cr", period="FY24")
    b = mk("revenue from services", "₹8,142 Cr", period="FY23", doc_id=2)
    assert rules.classify(a, b).label is None


def test_incomparable_units_produce_no_verdict():
    a = mk("revenue", "₹8,142 Cr", period="FY24")
    b = mk("revenue", "not disclosed", period="FY24", doc_id=2)
    v = rules.classify(a, b)
    assert v.label != rules.CORROBORATES


def test_a_fact_is_never_compared_with_itself():
    a = mk("revenue", "₹8,142 Cr", period="FY24")
    assert rules.classify(a, a).label is None


def test_agreement_under_differing_qualifiers_still_corroborates():
    """Same number reported consolidated and standalone: the qualifier did not change the
    answer, so the sources agree."""
    a = mk("pin-code reach", "18,793", period="Q4 FY24", scope="consolidated")
    b = mk("pin-code reach", "18,793", period="Q4 FY24", scope="standalone", doc_id=2)
    v = rules.classify(a, b)
    assert v.label == rules.CORROBORATES and "scope" in v.discriminator
